"""Stampede acceptance test: 100 concurrent 10-paise spends from a 500-paise wallet.

Run 20 times with `python -m scripts.stampede --runs 20`.
"""

import argparse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.db import pool, reset_test_data
from app.models import TransferCreate
from app.transfers import create_transfer_once


def _create_user(conn, name: str, email: str, phone: str, label: str) -> tuple[int, int]:
    user_id = conn.execute(
        """
        INSERT INTO users(name, email, phone, kyc_status)
        VALUES (%s, %s, %s, 'verified') RETURNING id
        """,
        (name, email, phone),
    ).fetchone()["id"]
    wallet_id = conn.execute(
        """
        INSERT INTO accounts(owner_user_id, kind, label, balance_paise)
        VALUES (%s, 'user_wallet', %s, 0) RETURNING id
        """,
        (user_id, label),
    ).fetchone()["id"]
    return user_id, wallet_id


def setup(run_number: int) -> tuple[int, int, int]:
    with pool.connection() as conn:
        reset_test_data(conn)
        source_user, source_wallet = _create_user(
            conn,
            f"Stampede sender {run_number}",
            f"stampede-sender-{run_number}@example.com",
            f"+9191000{run_number:06d}",
            "main",
        )
        _, destination_wallet = _create_user(
            conn,
            f"Stampede receiver {run_number}",
            f"stampede-receiver-{run_number}@example.com",
            f"+9192000{run_number:06d}",
            "main",
        )
        cash_in = conn.execute(
            "SELECT id FROM accounts WHERE kind = 'system_cash_in'"
        ).fetchone()["id"]
        transfer_id = conn.execute(
            """
            INSERT INTO transfers(type, status, amount_paise, source_account_id, destination_account_id)
            VALUES ('top_up', 'posted', 500, %s, %s) RETURNING id
            """,
            (cash_in, source_wallet),
        ).fetchone()["id"]
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO ledger_entries(transfer_id, account_id, direction, amount_paise)
                VALUES (%s, %s, %s, %s)
                """,
                [
                    (transfer_id, cash_in, "debit", 500),
                    (transfer_id, source_wallet, "credit", 500),
                ],
            )
        conn.execute(
            "UPDATE accounts SET balance_paise = balance_paise - 500 WHERE id = %s", (cash_in,)
        )
        conn.execute(
            "UPDATE accounts SET balance_paise = balance_paise + 500 WHERE id = %s", (source_wallet,)
        )
    return source_user, source_wallet, destination_wallet


def fire(
    source_user: int, source_wallet: int, destination_wallet: int, run_number: int, workers: int
) -> tuple[int, int]:
    def one(index: int) -> int:
        payload = TransferCreate(
            type="peer_transfer",
            amount_paise=10,
            source_wallet_id=source_wallet,
            destination_wallet_id=destination_wallet,
            note="stampede",
        )
        key = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"stampede-{run_number}-{index}"))
        return create_transfer_once(source_user, payload, key).status_code

    statuses: list[int] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(one, i) for i in range(100)]
        for future in as_completed(futures):
            statuses.append(future.result())
    return statuses.count(200), statuses.count(422)


def verify(source_wallet: int) -> tuple[int, int, int]:
    with pool.connection() as conn:
        balance = conn.execute(
            "SELECT balance_paise FROM accounts WHERE id = %s", (source_wallet,)
        ).fetchone()["balance_paise"]
        computed = conn.execute(
            """
            SELECT COALESCE(sum(amount_paise) FILTER (WHERE direction = 'credit'), 0)
                 - COALESCE(sum(amount_paise) FILTER (WHERE direction = 'debit'), 0) AS balance
            FROM ledger_entries WHERE account_id = %s
            """,
            (source_wallet,),
        ).fetchone()["balance"]
        totals = conn.execute(
            """
            SELECT
              COALESCE(sum(amount_paise) FILTER (WHERE direction = 'debit'), 0) AS debits,
              COALESCE(sum(amount_paise) FILTER (WHERE direction = 'credit'), 0) AS credits
            FROM ledger_entries
            """
        ).fetchone()
    return balance, computed, int(totals["credits"] - totals["debits"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--workers", type=int, default=100)
    args = parser.parse_args()

    if pool.closed:
        pool.open()
    failed = False
    try:
        for run in range(1, args.runs + 1):
            source_user, source_wallet, destination_wallet = setup(run)
            successes, insufficient = fire(
                source_user, source_wallet, destination_wallet, run, args.workers
            )
            balance, computed, global_delta = verify(source_wallet)
            ok = successes == 50 and insufficient == 50 and balance == 0 and computed == 0 and global_delta == 0
            print(
                f"run {run:02d}: success={successes} insufficient={insufficient} "
                f"stored={balance} computed={computed} global_delta={global_delta} "
                f"{'PASS' if ok else 'FAIL'}",
                flush=True,
            )
            failed = failed or not ok
    finally:
        pool.close()
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
