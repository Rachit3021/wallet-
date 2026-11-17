import uuid
from typing import Any

from fastapi.testclient import TestClient


def key() -> str:
    return str(uuid.uuid4())


def post(client: TestClient, path: str, body: dict[str, Any], idempotency_key: str | None = None):
    return client.post(
        path,
        json=body,
        headers={"Idempotency-Key": idempotency_key or key()},
    )


def make_user(client: TestClient, name: str, email: str, phone: str) -> dict[str, Any]:
    response = post(
        client,
        "/users",
        {"name": name, "email": email, "phone": phone, "kyc_status": "verified"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_wallet(client: TestClient, user_id: int, label: str = "main") -> dict[str, Any]:
    response = post(client, f"/users/{user_id}/wallets", {"label": label})
    assert response.status_code == 201, response.text
    return response.json()


def fund_wallet(conn, wallet_id: int, amount_paise: int, note: str = "test top up") -> int:
    """Insert a posted top-up directly, bypassing fraud rules for test setup."""
    cash_in = conn.execute(
        """
        SELECT id FROM accounts
        WHERE owner_user_id IS NULL AND kind = 'system_cash_in'
        """
    ).fetchone()["id"]
    transfer_id = conn.execute(
        """
        INSERT INTO transfers(type, status, amount_paise, source_account_id, destination_account_id, note)
        VALUES ('top_up', 'posted', %s, %s, %s, %s)
        RETURNING id
        """,
        (amount_paise, cash_in, wallet_id, note),
    ).fetchone()["id"]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO ledger_entries(transfer_id, account_id, direction, amount_paise)
            VALUES (%s, %s, %s, %s)
            """,
            [
                (transfer_id, cash_in, "debit", amount_paise),
                (transfer_id, wallet_id, "credit", amount_paise),
            ],
        )
    conn.execute(
        "UPDATE accounts SET balance_paise = balance_paise - %s WHERE id = %s",
        (amount_paise, cash_in),
    )
    conn.execute(
        "UPDATE accounts SET balance_paise = balance_paise + %s WHERE id = %s",
        (amount_paise, wallet_id),
    )
    return transfer_id
