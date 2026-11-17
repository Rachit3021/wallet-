from typing import Any

from app.errors import ApiError


def _rules(conn) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT id, code, description, rule_kind, threshold_paise, window_minutes, action
        FROM risk_rules
        WHERE enabled
        ORDER BY id
        """
    ).fetchall()


def evaluate(
    conn,
    transfer_id: int,
    source_account_id: int,
    transfer_type: str,
    amount_paise: int,
) -> list[dict[str, Any]]:
    """Run fraud queries inside the caller's transfer transaction."""
    fired: list[dict[str, Any]] = []
    for rule in _rules(conn):
        hits = False
        if rule["rule_kind"] == "amount":
            hits = amount_paise >= rule["threshold_paise"]
        elif rule["rule_kind"] == "velocity":
            row = conn.execute(
                """
                SELECT count(*) AS n
                FROM transfers
                WHERE source_account_id = %s
                  AND created_at >= now() - make_interval(mins => %s)
                """,
                (source_account_id, rule["window_minutes"]),
            ).fetchone()
            hits = row["n"] >= rule["threshold_paise"]
        elif rule["rule_kind"] == "outflow":
            row = conn.execute(
                """
                SELECT COALESCE(sum(amount_paise), 0) AS total
                FROM transfers
                WHERE source_account_id = %s
                  AND created_at >= now() - make_interval(mins => %s)
                """,
                (source_account_id, rule["window_minutes"]),
            ).fetchone()
            hits = row["total"] >= rule["threshold_paise"]
        if not hits:
            continue
        conn.execute(
            """
            INSERT INTO risk_flags(transfer_id, rule_id, action, highlighted_note)
            SELECT %s, %s, %s, t.note FROM transfers t WHERE t.id = %s
            """,
            (transfer_id, rule["id"], rule["action"], transfer_id),
        )
        fired.append(dict(rule))
    return fired


def has_hold(flags: list[dict[str, Any]]) -> bool:
    return any(flag["action"] == "hold" for flag in flags)


def explain_recent_flags(conn, client) -> int:
    """Add LLM explanations to flags created today. Used by the nightly job."""
    rows = conn.execute(
        """
        SELECT rf.id, rf.transfer_id, rr.code, rr.description,
               rr.threshold_paise, rr.window_minutes, rr.action, t.note
        FROM risk_flags rf
        JOIN risk_rules rr ON rr.id = rf.rule_id
        JOIN transfers t ON t.id = rf.transfer_id
        WHERE rf.created_at >= current_date AND rf.explanation IS NULL
        ORDER BY rf.id
        """
    ).fetchall()
    explained = 0
    for row in rows:
        source_id = conn.execute(
            "SELECT source_account_id FROM transfers WHERE id = %s", (row["transfer_id"],)
        ).fetchone()["source_account_id"]
        recent = conn.execute(
            """
            SELECT t.id, t.type, t.amount_paise, t.status, t.created_at, t.note
            FROM transfers t
            WHERE t.source_account_id = %s OR t.destination_account_id = %s
            ORDER BY t.created_at DESC, t.id DESC
            LIMIT 10
            """,
            (source_id, source_id),
        ).fetchall()
        try:
            result = client.explain_flag(
                {
                    "code": row["code"],
                    "description": row["description"],
                    "threshold_paise": row["threshold_paise"],
                    "window_minutes": row["window_minutes"],
                    "action": row["action"],
                },
                [dict(r) for r in recent],
            )
            label = result.get("label")
            if label not in ("likely_ok", "review", "likely_fraud"):
                label = "review"
            conn.execute(
                """
                UPDATE risk_flags
                SET explanation = %s, label = %s, reviewed = true
                WHERE id = %s AND explanation IS NULL
                """,
                (result.get("explanation", "Rule triggered"), label, row["id"]),
            )
            explained += 1
        except Exception:
            # One bad LLM response must not fail the nightly book check.
            continue
    return explained


def assert_release_allowed(conn, transfer_id: int) -> None:
    row = conn.execute(
        "SELECT status FROM transfers WHERE id = %s", (transfer_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "TRANSFER_NOT_FOUND", f"Transfer {transfer_id} does not exist")
    if row["status"] != "held":
        raise ApiError(409, "TRANSFER_NOT_HELD", "Only held transfers can be released")
