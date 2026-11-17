import base64
import json
from datetime import date

from app.errors import ApiError
from app.llm import get_client


def _decode_cursor(cursor: str | None) -> tuple[str | None, int | None]:
    if not cursor:
        return None, None
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return str(raw["created_at"]), int(raw["id"])
    except Exception:
        raise ApiError(422, "INVALID_CURSOR", "cursor is invalid")


def statement(conn, user_id: int, limit: int = 50, offset: int = 0, cursor: str | None = None) -> dict:
    if limit < 1 or limit > 200:
        raise ApiError(422, "INVALID_LIMIT", "limit must be between 1 and 200")
    if offset < 0:
        raise ApiError(422, "INVALID_OFFSET", "offset must be non-negative")
    created_at, entry_id = _decode_cursor(cursor)
    where_cursor = ""
    params: list = [user_id]
    if created_at and entry_id is not None:
        where_cursor = "AND (le.created_at, le.id) < (%s::timestamptz, %s)"
        params.extend([created_at, entry_id])
    params.extend([limit + 1, offset if not cursor else 0])
    rows = conn.execute(
        f"""
        SELECT le.id AS entry_id, le.created_at, le.direction, le.amount_paise,
               t.id AS transfer_id, t.type, t.status, t.note,
               a.id AS account_id, a.label AS account_label,
               other.id AS other_account_id, other.label AS other_label,
               other.name AS counterparty_name
        FROM ledger_entries le
        JOIN transfers t ON t.id = le.transfer_id
        JOIN accounts a ON a.id = le.account_id
        LEFT JOIN LATERAL (
          SELECT acc.id, acc.label, u.name
          FROM ledger_entries le2
          JOIN accounts acc ON acc.id = le2.account_id
          LEFT JOIN users u ON u.id = acc.owner_user_id
          WHERE le2.transfer_id = t.id AND le2.id <> le.id
          ORDER BY le2.id
          LIMIT 1
        ) other ON true
        WHERE a.owner_user_id = %s
          {where_cursor}
        ORDER BY le.created_at DESC, le.id DESC
        LIMIT %s OFFSET %s
        """,
        params,
    ).fetchall()
    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor = None
    if has_more and visible:
        last = visible[-1]
        raw = json.dumps(
            {"created_at": str(last["created_at"]), "id": last["entry_id"]}
        ).encode()
        next_cursor = base64.urlsafe_b64encode(raw).decode()
    return {
        "user_id": user_id,
        "pagination": {"limit": limit, "offset": offset, "cursor": cursor, "has_more": has_more, "next_cursor": next_cursor},
        "entries": [
            {
                "entry_id": r["entry_id"],
                "transfer_id": r["transfer_id"],
                "created_at": str(r["created_at"]),
                "direction": r["direction"],
                "amount_paise": r["amount_paise"],
                "type": r["type"],
                "status": r["status"],
                "account_id": r["account_id"],
                "account_label": r["account_label"],
                "counterparty": r["counterparty_name"] or r["other_label"],
                "note": r["note"],
            }
            for r in visible
        ],
    }


def _validated_query(question: str) -> dict:
    raw = get_client().parse_question(question)
    if not isinstance(raw, dict) or raw.get("type") not in (1, 2, 3, 4):
        raise ApiError(
            422,
            "UNSUPPORTED_QUESTION",
            "I can only answer: total sent, total received, largest transfer, or number of transfers",
        )
    try:
        start = date.fromisoformat(str(raw.get("from")))
        end = date.fromisoformat(str(raw.get("to")))
    except (TypeError, ValueError):
        raise ApiError(422, "UNSUPPORTED_QUESTION", "I need ISO dates in the question")
    if start > end:
        raise ApiError(422, "UNSUPPORTED_QUESTION", "The start date must not be after the end date")
    counterparty = raw.get("counterparty")
    if counterparty is not None:
        if not isinstance(counterparty, str) or not 1 <= len(counterparty) <= 100:
            raise ApiError(422, "UNSUPPORTED_QUESTION", "Counterparty name is invalid")
    return {"type": int(raw["type"]), "from": start, "to": end, "counterparty": counterparty}


def _count_and_ids(rows) -> tuple[int, list[int]]:
    row = rows[0]
    return int(row["total"] or 0), [int(i) for i in (row["ids"] or [])]


def _run_query(conn, user_id: int, query: dict) -> tuple[int, list[int]]:
    start, end, counterparty = query["from"], query["to"], query["counterparty"]
    if query["type"] == 1:
        rows = conn.execute(
            """
            SELECT COALESCE(sum(le.amount_paise), 0) AS total,
                   COALESCE(array_agg(DISTINCT t.id) FILTER (WHERE t.id IS NOT NULL), '{}') AS ids
            FROM ledger_entries le
            JOIN transfers t ON t.id = le.transfer_id
            JOIN accounts src ON src.id = le.account_id
            JOIN accounts dst ON dst.id = t.destination_account_id
            LEFT JOIN users u ON u.id = dst.owner_user_id
            WHERE src.owner_user_id = %s
              AND le.direction = 'debit'
              AND t.type = 'peer_transfer'
              AND t.created_at::date BETWEEN %s AND %s
              AND (%s::text IS NULL OR u.name ILIKE '%%' || %s::text || '%%')
            """,
            (user_id, start, end, counterparty, counterparty),
        ).fetchall()
        return _count_and_ids(rows)

    if query["type"] == 2:
        rows = conn.execute(
            """
            SELECT COALESCE(sum(le.amount_paise), 0) AS total,
                   COALESCE(array_agg(DISTINCT t.id) FILTER (WHERE t.id IS NOT NULL), '{}') AS ids
            FROM ledger_entries le
            JOIN transfers t ON t.id = le.transfer_id
            JOIN accounts dst ON dst.id = le.account_id
            JOIN accounts src ON src.id = t.source_account_id
            LEFT JOIN users u ON u.id = src.owner_user_id
            WHERE dst.owner_user_id = %s
              AND le.direction = 'credit'
              AND t.created_at::date BETWEEN %s AND %s
              AND (%s::text IS NULL OR u.name ILIKE '%%' || %s::text || '%%')
            """,
            (user_id, start, end, counterparty, counterparty),
        ).fetchall()
        return _count_and_ids(rows)

    if query["type"] == 3:
        rows = conn.execute(
            """
            WITH involved AS (
              SELECT le.transfer_id, le.amount_paise
              FROM ledger_entries le
              JOIN accounts a ON a.id = le.account_id
              WHERE a.owner_user_id = %s
                AND le.created_at::date BETWEEN %s AND %s
            )
            SELECT COALESCE(max(amount_paise), 0) AS total,
                   COALESCE(array_agg(DISTINCT transfer_id) FILTER (
                     WHERE transfer_id IS NOT NULL AND amount_paise = (SELECT max(amount_paise) FROM involved)
                   ), '{}') AS ids
            FROM involved
            """,
            (user_id, start, end),
        ).fetchall()
        return _count_and_ids(rows)

    rows = conn.execute(
        """
        SELECT count(DISTINCT t.id) AS total,
               COALESCE(array_agg(DISTINCT t.id) FILTER (WHERE t.id IS NOT NULL), '{}') AS ids
        FROM transfers t
        JOIN ledger_entries le ON le.transfer_id = t.id
        JOIN accounts a ON a.id = le.account_id
        WHERE a.owner_user_id = %s
          AND t.created_at::date BETWEEN %s AND %s
        """,
        (user_id, start, end),
    ).fetchall()
    return _count_and_ids(rows)


def ask(conn, user_id: int, question: str) -> tuple[int, dict]:
    if conn.execute("SELECT id FROM users WHERE id = %s", (user_id,)).fetchone() is None:
        raise ApiError(404, "USER_NOT_FOUND", f"User {user_id} does not exist")
    query = _validated_query(question)
    total, ids = _run_query(conn, user_id, query)
    labels = {1: "paise_sent", 2: "paise_received", 3: "largest_transfer_paise", 4: "transfer_count"}
    return 200, {
        "answer": {labels[query["type"]]: total},
        "amount_paise": total,
        "currency": "INR",
        "transfer_ids": ids,
        "query": {
            "type": query["type"],
            "counterparty": query["counterparty"],
            "from": query["from"].isoformat(),
            "to": query["to"].isoformat(),
        },
        "scope": f"Only user {user_id}'s accounts were queried",
    }
