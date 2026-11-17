import hashlib
import json
import uuid
from typing import Any, Callable

from fastapi import Header
from fastapi.responses import JSONResponse

from app.db import transaction
from app.errors import ApiError, error_payload


def require_key(
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> str:
    try:
        uuid.UUID(idempotency_key)
    except (TypeError, ValueError):
        raise ApiError(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be a UUID")
    return idempotency_key


def body_hash(body: Any) -> str:
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _hash_for_key(key: str) -> str:
    return body_hash({"key": key})


def begin(conn, user_id: int | None, key: str, request_hash: str) -> tuple[int | None, JSONResponse | None]:
    """Insert the idempotency key or return the saved response for a replay."""
    inserted = conn.execute(
        """
        INSERT INTO idempotency_keys(user_id, key, request_hash, status)
        VALUES (%s, %s, %s, 'processing')
        ON CONFLICT (user_id, key) DO NOTHING
        RETURNING id
        """,
        (user_id, key, request_hash),
    ).fetchone()

    if inserted:
        return inserted["id"], None

    existing = conn.execute(
        """
        SELECT request_hash, status, response_code, response_body
        FROM idempotency_keys
        WHERE user_id IS NOT DISTINCT FROM %s AND key = %s
        """,
        (user_id, key),
    ).fetchone()

    if existing is None:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "Unable to resolve idempotency key")

    if existing["request_hash"] != request_hash:
        raise ApiError(
            422,
            "IDEMPOTENCY_KEY_REUSED",
            "This idempotency key was already used with a different request body",
        )

    if existing["status"] == "completed" and existing["response_body"] is not None:
        return None, JSONResponse(
            status_code=existing["response_code"],
            content=existing["response_body"],
        )

    raise ApiError(409, "REQUEST_IN_PROGRESS", "The first request with this key is still running")


def finish(conn, idempotency_id: int, status_code: int, response_body: dict[str, Any]) -> None:
    conn.execute(
        """
        UPDATE idempotency_keys
        SET status = 'completed', response_code = %s, response_body = %s::jsonb, completed_at = now()
        WHERE id = %s
        """,
        (status_code, json.dumps(response_body), idempotency_id),
    )


def run_idempotent(
    key: str,
    user_id: int | None,
    request_body: Any,
    operation: Callable[[Any], tuple[int, dict[str, Any]]],
) -> JSONResponse:
    """Run a POST operation exactly once and save its first response.

    Business failures are saved too, so a client retry sees the original result.
    Unexpected exceptions roll the whole transaction back and are retriable.
    """
    request_hash = body_hash(request_body)
    with transaction() as conn:
        idempotency_id, replay = begin(conn, user_id, key, request_hash)
        if replay is not None:
            return replay
        assert idempotency_id is not None
        try:
            status_code, response_body = operation(conn)
        except ApiError as exc:
            error_body = error_payload(exc.code, exc.message, str(uuid.uuid4()))
            finish(conn, idempotency_id, exc.status_code, error_body)
            return JSONResponse(status_code=exc.status_code, content=error_body)
        finish(conn, idempotency_id, status_code, response_body)
        return JSONResponse(status_code=status_code, content=response_body)
