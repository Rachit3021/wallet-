"""Seed a realistic wallet database.

Defaults create 1,000 users, 2,000 wallets and 100,000 transfers. Pass flags for
a smaller smoke-test dataset, e.g. `python -m scripts.seed --transfers 200`.
"""

import argparse
import os
import random
from datetime import datetime, timedelta, timezone

import psycopg

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://wallet:wallet@localhost:5432/wallet")
BATCH = 2000


def _placeholders(rows: list[tuple]) -> tuple[str, list]:
    if not rows:
        return "", []
    width = len(rows[0])
    row_sql = "(" + ",".join(["%s"] * width) + ")"
    return ",".join([row_sql] * len(rows)), [value for row in rows for value in row]


def truncate(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            TRUNCATE TABLE
              reconciliation_problems, reconciliation_runs, transfer_status_audit,
              risk_flags, idempotency_keys, ledger_entries, transfers, accounts,
              users, risk_rules, job_locks
            RESTART IDENTITY CASCADE
            """
        )
        cur.execute(
            """
            INSERT INTO accounts(kind, label, balance_paise) VALUES
              ('system_cash_in', 'external cash-in', 0),
              ('system_cash_out', 'external cash-out', 0),
              ('system_fee_revenue', 'fee revenue', 0)
            """
        )
        cur.execute("INSERT INTO job_locks(name) VALUES ('reconciliation')")
        cur.execute(
            """
            INSERT INTO risk_rules(code, description, rule_kind, threshold_paise, window_minutes, action) VALUES
              ('LARGE_TRANSFER', 'Hold a single transfer at or above the configured amount', 'amount', 500000, 0, 'hold'),
              ('VELOCITY_10M', 'Hold when one source account starts too many transfers in the window', 'velocity', 1000, 10, 'hold'),
              ('DAILY_OUTFLOW', 'Flag when one source account sends too much in the window', 'outflow', 2000000, 1440, 'flag')
            """
        )
    conn.commit()


def seed_users_and_wallets(conn, user_count: int, wallets_per_user: int):
    with conn.cursor() as cur:
        user_rows = [
            (f"User {i}", f"user{i}@example.com", f"+910000{i:07d}", "verified")
            for i in range(1, user_count + 1)
        ]
        values, params = _placeholders(user_rows)
        cur.execute(
            f"INSERT INTO users(name, email, phone, kyc_status) VALUES {values} RETURNING id",
            params,
        )
        user_ids = [row[0] for row in cur.fetchall()]

        wallet_rows = []
        for user_id in user_ids:
            for index in range(wallets_per_user):
                label = "main" if index == 0 else f"savings-{index}"
                wallet_rows.append((user_id, label))
        values, params = _placeholders(wallet_rows)
        cur.execute(
            f"""
            INSERT INTO accounts(owner_user_id, kind, label, balance_paise)
            SELECT v.user_id, 'user_wallet', v.label, 0
            FROM (VALUES {values}) AS v(user_id, label)
            RETURNING id
            """,
            params,
        )
        wallet_ids = [row[0] for row in cur.fetchall()]

        system = cur.execute(
            """
            SELECT id, kind FROM accounts
            WHERE owner_user_id IS NULL AND kind IN ('system_cash_in', 'system_cash_out', 'system_fee_revenue')
            """
        ).fetchall()
        by_kind = {row[1]: row[0] for row in system}
    conn.commit()
    return (
        user_ids,
        wallet_ids,
        by_kind["system_cash_in"],
        by_kind["system_cash_out"],
        by_kind["system_fee_revenue"],
    )


def build_transfer_specs(wallet_ids: list[int], cash_in_id: int, transfer_count: int):
    specs = []
    top_ups = min(len(wallet_ids), transfer_count)
    now = datetime.now(timezone.utc)
    for i in range(top_ups):
        specs.append(
            (
                "top_up",
                1_000_000,
                cash_in_id,
                wallet_ids[i],
                "initial top-up",
                now - timedelta(days=30, minutes=i),
            )
        )
    for i in range(top_ups, transfer_count):
        source = random.choice(wallet_ids)
        destination = random.choice(wallet_ids)
        while destination == source:
            destination = random.choice(wallet_ids)
        specs.append(
            (
                "peer_transfer",
                random.randint(100, 2000),
                source,
                destination,
                f"seed transfer {i}",
                now - timedelta(days=random.randint(0, 29), minutes=random.randint(0, 1439)),
            )
        )
    return specs


def insert_transfers(conn, specs) -> None:
    with conn.cursor() as cur:
        for start in range(0, len(specs), BATCH):
            chunk = specs[start : start + BATCH]
            transfer_rows = [
                (kind, "posted", amount, source, dest, note, created, created)
                for kind, amount, source, dest, note, created in chunk
            ]
            values, params = _placeholders(transfer_rows)
            cur.execute(
                f"""
                INSERT INTO transfers(
                  type, status, amount_paise, source_account_id,
                  destination_account_id, note, created_at, updated_at
                ) VALUES {values}
                RETURNING id, amount_paise, source_account_id, destination_account_id
                """,
                params,
            )
            inserted = cur.fetchall()
            entry_rows = []
            audit_rows = []
            for transfer_id, amount, source, dest in inserted:
                entry_rows.append((transfer_id, source, "debit", amount))
                entry_rows.append((transfer_id, dest, "credit", amount))
                audit_rows.append((transfer_id, None, "posted", "seed"))

            values, params = _placeholders(entry_rows)
            cur.execute(
                f"""
                INSERT INTO ledger_entries(transfer_id, account_id, direction, amount_paise)
                VALUES {values}
                """,
                params,
            )
            values, params = _placeholders(audit_rows)
            cur.execute(
                f"""
                INSERT INTO transfer_status_audit(transfer_id, from_status, to_status, actor)
                VALUES {values}
                """,
                params,
            )

        cur.execute(
            """
            UPDATE accounts a
            SET balance_paise = COALESCE(c.computed, 0)
            FROM (
              SELECT account_id,
                     COALESCE(sum(amount_paise) FILTER (WHERE direction = 'credit'), 0)
                     - COALESCE(sum(amount_paise) FILTER (WHERE direction = 'debit'), 0) AS computed
              FROM ledger_entries
              GROUP BY account_id
            ) c
            WHERE c.account_id = a.id
            """
        )
        # Bulk inserts do not produce autovacuum statistics immediately. Without
        # these, the planner can choose a primary-key scan for the statement
        # counterparty lookup and scan the whole ledger for every entry.
        for table in ("users", "accounts", "transfers", "ledger_entries"):
            cur.execute(f"ANALYZE {table}")
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=1000)
    parser.add_argument("--wallets-per-user", type=int, default=2)
    parser.add_argument("--transfers", type=int, default=100000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    random.seed(args.seed)

    with psycopg.connect(DATABASE_URL) as conn:
        truncate(conn)
        user_ids, wallet_ids, cash_in, _, _ = seed_users_and_wallets(
            conn, args.users, args.wallets_per_user
        )
        specs = build_transfer_specs(wallet_ids, cash_in, args.transfers)
        insert_transfers(conn, specs)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ledger_entries")
            entries = cur.fetchone()[0]
            cur.execute("SELECT min(balance_paise) FROM accounts WHERE owner_user_id IS NOT NULL")
            min_balance = cur.fetchone()[0]
    print(
        f"seeded {len(user_ids)} users, {len(wallet_ids)} wallets, "
        f"{args.transfers} transfers, {entries} ledger entries, "
        f"minimum user balance {min_balance} paise"
    )


if __name__ == "__main__":
    main()
