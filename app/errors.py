from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_payload(code: str, message: str, request_id: str) -> dict[str, Any]:
    return {"code": code, "message": message, "request_id": request_id}


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    import uuid

    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(exc.code, exc.message, str(uuid.uuid4())),
    )


async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    import uuid

    errors = exc.errors()
    missing_key = any(
        str(err.get("loc", ())[-1]).lower() in ("idempotency-key", "idempotency_key")
        for err in errors
    )
    if missing_key:
        return JSONResponse(
            status_code=400,
            content=error_payload(
                "IDEMPOTENCY_KEY_REQUIRED",
                "Idempotency-Key header is required on every POST request",
                str(uuid.uuid4()),
            ),
        )
    return JSONResponse(
        status_code=422,
        content=error_payload(
            "VALIDATION_ERROR",
            "; ".join(
                f"{'.'.join(str(part) for part in err.get('loc', ()))}: {err.get('msg')}"
                for err in errors
            ),
            str(uuid.uuid4()),
        ),
    )
