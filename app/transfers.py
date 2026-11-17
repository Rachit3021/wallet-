import random
import threading
import time
from typing import Any

from fastapi.responses import JSONResponse
from psycopg.errors import DeadlockDetected, SerializationFailure

from app import accounts, fraud
from app.errors import ApiError
from app.idempotency import run_idempotent
from app.models import ReleaseRequest, TransferCreate

# Serialise whole transfer transactions inside one process. Waiting threads do
# not hold a DB transaction while they wait, so this cannot deadlock with row
# locks. Cross-process safety still comes from the DB row locks and retries.
_TRANSFER_LOCK = threading.Lock()


def _actor(conn, user_id: int | None) -> str:
    if user_id is None:
        return "system"
    row = conn.execute("SELECT name FROM users WHERE id = %s", (user_id,)).fetchone()
    return row["name"] if row else f"user:{user_id}"


def _wallet(conn, wallet_id: int | None, user_id: int | None, label: str) -> int:
    if wallet_id is None:
        raise ApiError(422, "WALLET_REQUIRED", f"{label} is required for this transfer type")
    row = conn.execute(
        """
        SELECT id, owner_user_id, kind FROM accounts
        WHERE id = %s AND kind = 'user_wallet'
        """,
        (wallet_id,),
    ).fetchone()
    if row is None:
        raise ApiError(404, "WALLET_NOT_FOUND", f"Wallet {wallet_id} does not exist")
    if user_id is not None and row["owner_user_id"] != user_id:
        raise ApiError(403, "WALLET_FORBIDDEN", "This wallet belongs to another user")
    return row["id"]


def _resolve_accounts(conn, payload: TransferCreate, user_id: int | None) -> tuple[int, int, int | None]:
    original_id = None
    if payload.type == "top_up":
        return accounts.system_account(conn, "system_cash_in"), _wallet(
            conn, payload.destination_wallet_id, user_id, "destination_wallet_id"
        ), original_id
    if payload.type == "peer_transfer":
        source = _wallet(conn, payload.source_wallet_id, user_id, "source_wallet_id")
        destination = _wallet(conn, payload.destination_wallet_id, None, "destination_wallet_id")
        if source == destination:
            raise ApiError(422, "SAME_WALLET", "Cannot transfer to the same wallet")
        return source, destination, original_id
    if payload.type == "withdrawal":
        return _wallet(conn, payload.source_wallet_id, user_id, "source_wallet_id"), accounts.system_account(
            conn, "system_cash_out"
        ), original_id
    if payload.type == "fee":
        return _wallet(conn, payload.source_wallet_id, user_id, "source_wallet_id"), accounts.system_account(
            conn, "system_fee_revenue"
        ), original_id
    if payload.type == "reversal":
        if payload.original_transfer_id is None:
            raise ApiError(422, "ORIGINAL_REQUIRED", "original_transfer_id is required for a reversal")
        original = conn.execute(
            "SELECT id, status FROM transfers WHERE id = %s", (payload.original_transfer_id,)
        ).fetchone()
        if original is None:
            raise ApiError(404, "TRANSFER_NOT_FOUND", "Original transfer does not exist")
        if original["status"] != "posted":
            raise ApiError(409, "TRANSFER_NOT_REVERSIBLE", "Only a posted transfer can be reversed")
        already = conn.execute(
            "SELECT id FROM transfers WHERE original_transfer_id = %s", (original["id"],)
        ).fetchone()
        if already:
            raise ApiError(409, "ALREADY_REVERSED", "This transfer has already been reversed")
        debit = conn.execute(
            """
            SELECT account_id FROM ledger_entries
            WHERE transfer_id = %s AND direction = 'debit' ORDER BY id LIMIT 1
            """,
            (original["id"],),
        ).fetchone()
        credit = conn.execute(
            """
            SELECT account_id FROM ledger_entries
            WHERE transfer_id = %s AND direction = 'credit' ORDER BY id LIMIT 1
            """,
            (original["id"],),
        ).fetchone()
        if debit is None or credit is None:
            raise ApiError(409, "TRANSFER_NOT_REVERSIBLE", "Original transfer has no balanced entries")
        original_id = original["id"]
        return credit["account_id"], debit["account_id"], original_id
    raise ApiError(422, "UNSUPPORTED_TRANSFER_TYPE", "Unsupported transfer type")


def _set_status(conn, transfer_id: int, new_status: str, actor: str) -> None:
    row = conn.execute(
        "SELECT status FROM transfers WHERE id = %s FOR UPDATE", (transfer_id,)
    ).fetchone()
    old_status = row["status"]
    conn.execute(
        "UPDATE transfers SET status = %s, updated_at = now() WHERE id = %s",
        (new_status, transfer_id),
    )
    conn.execute(
        """
        INSERT INTO transfer_status_audit(transfer_id, from_status, to_status, actor)
        VALUES (%s, %s, %s, %s)
        """,
        (transfer_id, old_status, new_status, actor),
    )


def _post_transfer(conn, transfer_id: int, actor: str) -> None:
    """Lock both rows in ascending id order, re-check balance, then write entries."""
    transfer = conn.execute(
        """
        SELECT id, amount_paise, source_account_id, destination_account_id
        FROM transfers WHERE id = %s FOR UPDATE
        """,
        (transfer_id,),
    ).fetchone()
    account_ids = sorted({transfer["source_account_id"], transfer["destination_account_id"]})
    # Lock rows one at a time in ascending id order. Whole transfer transactions
    # are also serialised in-process by _TRANSFER_LOCK, so waiting callers never
    # hold a DB transaction while queued.
    by_id = {}
    for account_id in account_ids:
        row = conn.execute(
            """
            SELECT id, owner_user_id, balance_paise
            FROM accounts
            WHERE id = %s
            FOR UPDATE
            """,
            (account_id,),
        ).fetchone()
        if row is None:
            raise ApiError(500, "ACCOUNT_MISSING", "A transfer account is missing")
        by_id[row["id"]] = row
    source = by_id[transfer["source_account_id"]]
    if source["owner_user_id"] is not None and source["balance_paise"] < transfer["amount_paise"]:
        raise ApiError(
            422,
            "INSUFFICIENT_FUNDS",
            f"Wallet has {source['balance_paise']} paise; {transfer['amount_paise']} required",
        )

    conn.execute(
        "UPDATE accounts SET balance_paise = balance_paise - %s WHERE id = %s",
        (transfer["amount_paise"], transfer["source_account_id"]),
    )
    conn.execute(
        "UPDATE accounts SET balance_paise = balance_paise + %s WHERE id = %s",
        (transfer["amount_paise"], transfer["destination_account_id"]),
    )
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO ledger_entries(transfer_id, account_id, direction, amount_paise)
            VALUES (%s, %s, %s, %s)
            """,
            [
                (transfer_id, transfer["source_account_id"], "debit", transfer["amount_paise"]),
                (transfer_id, transfer["destination_account_id"], "credit", transfer["amount_paise"]),
            ],
        )
    _set_status(conn, transfer_id, "posted", actor)
    if conn.execute(
        "SELECT original_transfer_id FROM transfers WHERE id = %s", (transfer_id,)
    ).fetchone()["original_transfer_id"] is not None:
        original = conn.execute(
            "SELECT original_transfer_id FROM transfers WHERE id = %s", (transfer_id,)
        ).fetchone()["original_transfer_id"]
        _set_status(conn, original, "reversed", actor)


def _response(conn, transfer_id: int, flags: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    row = conn.execute(
        """
        SELECT id, type, status, amount_paise, source_account_id,
               destination_account_id, original_transfer_id, created_at
        FROM transfers WHERE id = %s
        """,
        (transfer_id,),
    ).fetchone()
    return 200 if row["status"] == "posted" else 202, {
        "transfer_id": row["id"],
        "type": row["type"],
        "status": row["status"],
        "amount_paise": row["amount_paise"],
        "source_account_id": row["source_account_id"],
        "destination_account_id": row["destination_account_id"],
        "original_transfer_id": row["original_transfer_id"],
        "flags": [
            {"code": flag["code"], "action": flag["action"]} for flag in flags
        ],
        "created_at": str(row["created_at"]),
    }


def create_transfer(conn, payload: TransferCreate, user_id: int | None) -> tuple[int, dict[str, Any]]:
    source, destination, original_id = _resolve_accounts(conn, payload, user_id)
    actor = _actor(conn, user_id)
    row = conn.execute(
        """
        INSERT INTO transfers(
          type, status, amount_paise, source_account_id, destination_account_id,
          initiated_by_user_id, original_transfer_id, note
        )
        VALUES (%s, 'pending', %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            payload.type,
            payload.amount_paise,
            source,
            destination,
            user_id,
            original_id,
            payload.note,
        ),
    ).fetchone()
    transfer_id = row["id"]
    _set_status(conn, transfer_id, "pending", actor)

    flags = fraud.evaluate(conn, transfer_id, source, payload.type, payload.amount_paise)
    if fraud.has_hold(flags):
        _set_status(conn, transfer_id, "held", actor)
        return _response(conn, transfer_id, flags)

    _post_transfer(conn, transfer_id, actor)
    return _response(conn, transfer_id, flags)


def release_transfer(conn, transfer_id: int, actor: str) -> tuple[int, dict[str, Any]]:
    fraud.assert_release_allowed(conn, transfer_id)
    conn.execute("UPDATE risk_flags SET reviewed = true WHERE transfer_id = %s", (transfer_id,))
    _post_transfer(conn, transfer_id, actor)
    return _response(conn, transfer_id, [])


def _idempotent_with_retry(key: str, user_id: int | None, body: Any, operation) -> JSONResponse:
    """Retry retriable DB errors. A rolled-back retry is safe: the key rolled back too."""
    for attempt in range(1, 26):
        try:
            return run_idempotent(key, user_id, body, operation)
        except (DeadlockDetected, SerializationFailure):
            if attempt == 25:
                raise
            time.sleep(min(0.2, 0.01 * attempt) + random.random() * 0.01)
    raise AssertionError("unreachable")


def create_transfer_once(user_id: int, payload: TransferCreate, key: str) -> JSONResponse:
    with _TRANSFER_LOCK:
        return _idempotent_with_retry(
            key,
            user_id,
            payload.model_dump(mode="json"),
            lambda conn: create_transfer(conn, payload, user_id),
        )


def release_transfer_once(transfer_id: int, body: ReleaseRequest, key: str) -> JSONResponse:
    with _TRANSFER_LOCK:
        return _idempotent_with_retry(
            key,
            None,
            body.model_dump(mode="json"),
            lambda conn: release_transfer(conn, transfer_id, body.actor),
        )
