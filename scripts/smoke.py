import time
import uuid

import httpx

from app.db import connection, pool, reset_test_data

BASE = "http://localhost:8000"


def key():
    return str(uuid.uuid4())


def post(client, path, body):
    response = client.post(path, json=body, headers={"Idempotency-Key": key()})
    print("POST", path, response.status_code, response.json())
    response.raise_for_status()
    return response.json()


def main():
    if pool.closed:
        pool.open()
    with connection() as conn:
        reset_test_data(conn)
    with httpx.Client(base_url=BASE, timeout=10) as client:
        for _ in range(20):
            try:
                client.get("/health").raise_for_status()
                break
            except Exception:
                time.sleep(0.5)
        alice = post(client, "/users", {"name": "Asha", "email": "asha@example.com", "phone": "+911111111111", "kyc_status": "verified"})
        bob = post(client, "/users", {"name": "Rahul", "email": "rahul@example.com", "phone": "+922222222222", "kyc_status": "verified"})
        alice_wallet = post(client, f"/users/{alice['id']}/wallets", {"label": "main"})
        bob_wallet = post(client, f"/users/{bob['id']}/wallets", {"label": "main"})
        post(client, f"/users/{alice['id']}/transfers", {
            "type": "top_up", "amount_paise": 100000,
            "destination_wallet_id": alice_wallet["id"], "note": "smoke top up",
        })
        post(client, f"/users/{alice['id']}/transfers", {
            "type": "peer_transfer", "amount_paise": 50000,
            "source_wallet_id": alice_wallet["id"],
            "destination_wallet_id": bob_wallet["id"], "note": "sent to Rahul",
        })
        print("alice balance", client.get(f"/users/{alice['id']}/balance").json())
        print("bob balance", client.get(f"/users/{bob['id']}/balance").json())
        print("alice statement", client.get(f"/users/{alice['id']}/statement").json())
        print("ask", post(client, f"/users/{alice['id']}/statement/ask", {"question": "How much did I send to Rahul in the last 7 days?"}))


if __name__ == "__main__":
    main()
