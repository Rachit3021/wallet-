from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import accounts, statement, transfers
from app.db import connection, pool
from app.errors import ApiError, api_error_handler, validation_error_handler
from app.idempotency import require_key, run_idempotent
from app.models import (
    AskRequest,
    EmptyRequest,
    ReleaseRequest,
    TransferCreate,
    UserCreate,
    WalletCreate,
)
from app.reconciliation import reconcile


@asynccontextmanager
async def lifespan(_: FastAPI):
    if pool.closed:
        pool.open()
    yield
    if not pool.closed:
        pool.close()


app = FastAPI(title="Digital Wallet", version="1.0.0", lifespan=lifespan)
app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)


@app.get("/health")
def health():
    with connection() as conn:
        row = conn.execute(
            "SELECT version() AS v, now() AS t, (SELECT count(*) FROM accounts) AS accounts"
        ).fetchone()
    return {
        "status": "ok",
        "db": row["v"],
        "db_time": str(row["t"]),
        "accounts": row["accounts"],
    }


@app.post("/users", status_code=201)
def create_user(
    body: UserCreate,
    key: str = Depends(require_key),
):
    return run_idempotent(
        key,
        None,
        body.model_dump(mode="json"),
        lambda conn: (201, accounts.create_user(conn, body)),
    )


@app.post("/users/{user_id}/wallets", status_code=201)
def create_wallet(
    user_id: int,
    body: WalletCreate,
    key: str = Depends(require_key),
):
    return run_idempotent(
        key,
        user_id,
        body.model_dump(mode="json"),
        lambda conn: (201, accounts.create_wallet(conn, user_id, body.label)),
    )


@app.get("/users/{user_id}/balance")
def get_balance(user_id: int):
    with connection() as conn:
        return accounts.balance(conn, user_id)


@app.get("/users/{user_id}/statement")
def get_statement(
    user_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    cursor: str | None = None,
):
    with connection() as conn:
        return statement.statement(conn, user_id, limit=limit, offset=offset, cursor=cursor)


@app.post("/users/{user_id}/statement/ask")
def ask_statement(
    user_id: int,
    body: AskRequest,
    key: str = Depends(require_key),
):
    return run_idempotent(
        key,
        user_id,
        body.model_dump(mode="json"),
        lambda conn: statement.ask(conn, user_id, body.question),
    )


@app.post("/users/{user_id}/transfers", status_code=200)
def create_transfer(
    user_id: int,
    body: TransferCreate,
    key: str = Depends(require_key),
):
    return transfers.create_transfer_once(user_id, body, key)


@app.post("/transfers", status_code=200)
def create_transfer_with_user(
    user_id: int = Query(...),
    body: TransferCreate = ...,
    key: str = Depends(require_key),
):
    return transfers.create_transfer_once(user_id, body, key)


@app.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: int):
    with connection() as conn:
        row = conn.execute(
            """
            SELECT id, type, status, amount_paise, source_account_id,
                   destination_account_id, original_transfer_id, note,
                   created_at, updated_at
            FROM transfers
            WHERE id = %s
            """,
            (transfer_id,),
        ).fetchone()
    if row is None:
        raise ApiError(404, "TRANSFER_NOT_FOUND", f"Transfer {transfer_id} does not exist")
    return {
        "id": row["id"],
        "type": row["type"],
        "status": row["status"],
        "amount_paise": row["amount_paise"],
        "source_account_id": row["source_account_id"],
        "destination_account_id": row["destination_account_id"],
        "original_transfer_id": row["original_transfer_id"],
        "note": row["note"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


@app.post("/transfers/{transfer_id}/release")
def release_transfer(
    transfer_id: int,
    body: ReleaseRequest,
    key: str = Depends(require_key),
):
    return transfers.release_transfer_once(transfer_id, body, key)


@app.post("/admin/reconciliation/run")
def run_reconciliation(
    _: EmptyRequest,
    key: str = Depends(require_key),
):
    return run_idempotent(
        key,
        None,
        {},
        lambda _conn: (200, reconcile(triggered_by="manual")),
    )


@app.get("/admin/reconciliation/runs")
def reconciliation_runs(limit: int = Query(20, ge=1, le=100)):
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id, started_at, ended_at, status, counts
            FROM reconciliation_runs
            ORDER BY id DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "started_at": str(r["started_at"]),
            "ended_at": str(r["ended_at"]) if r["ended_at"] else None,
            "status": r["status"],
            "counts": r["counts"],
        }
        for r in rows
    ]


@app.get("/admin/reconciliation/problems")
def reconciliation_problems(run_id: int | None = None, limit: int = Query(100, ge=1, le=500)):
    with connection() as conn:
        if run_id is None:
            rows = conn.execute(
                """
                SELECT run_id, check_name, entity_id, expected, actual
                FROM reconciliation_problems ORDER BY id DESC LIMIT %s
                """,
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT run_id, check_name, entity_id, expected, actual
                FROM reconciliation_problems WHERE run_id = %s ORDER BY id
                """,
                (run_id,),
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/admin/risk-flags")
def risk_flags(limit: int = Query(100, ge=1, le=500)):
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT rf.id, rf.transfer_id, rf.action, rf.label, rf.explanation,
                   rr.code, rr.description, rf.created_at
            FROM risk_flags rf JOIN risk_rules rr ON rr.id = rf.rule_id
            ORDER BY rf.id DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [{**dict(r), "created_at": str(r["created_at"])} for r in rows]
