import json
from typing import Any

from psycopg import IsolationLevel
from psycopg.errors import LockNotAvailable

from app.db import transaction
from app.fraud import explain_recent_flags
from app.llm import get_client


def _add_problem(problems: list[dict[str, Any]], check: str, entity_id: int | None, expected: Any, actual: Any) -> None:
    problems.append(
        {
            "check_name": check,
            "entity_id": entity_id,
            "expected": str(expected),
            "actual": str(actual),
        }
    )


def reconcile(triggered_by: str = "scheduler") -> dict[str, Any]:
    """Run every book check in one REPEATABLE READ transaction.

    The job only records problems; it never mutates balances or ledger rows.
    """
    with transaction(IsolationLevel.REPEATABLE_READ) as conn:
        try:
            lock = conn.execute(
                """
                SELECT id FROM job_locks
                WHERE name = 'reconciliation'
                FOR UPDATE NOWAIT
                """
            ).fetchone()
        except LockNotAvailable:
            return {"skipped": True, "reason": "reconciliation is already running"}
        if lock is None:
            return {"skipped": True, "reason": "job lock is missing"}

        run = conn.execute(
            """
            INSERT INTO reconciliation_runs(started_at, status)
            VALUES (now(), 'running')
            RETURNING id
            """
        ).fetchone()
        run_id = run["id"]
        problems: list[dict[str, Any]] = []
        counts: dict[str, Any] = {}

        global_row = conn.execute(
            """
            SELECT
              COALESCE(sum(amount_paise) FILTER (WHERE direction = 'debit'), 0) AS debits,
              COALESCE(sum(amount_paise) FILTER (WHERE direction = 'credit'), 0) AS credits
            FROM ledger_entries
            """
        ).fetchone()
        counts["global_debits"] = int(global_row["debits"] or 0)
        counts["global_credits"] = int(global_row["credits"] or 0)

        bad_entries = conn.execute(
            """
            SELECT le.id, le.account_id, le.transfer_id,
                   t.amount_paise AS transfer_amount,
                   le.amount_paise AS entry_amount
            FROM ledger_entries le
            JOIN transfers t ON t.id = le.transfer_id
            WHERE le.amount_paise <> t.amount_paise
            ORDER BY le.id
            """
        ).fetchall()
        counts["entry_amount_mismatches"] = len(bad_entries)
        handled_transfers = {row["transfer_id"] for row in bad_entries}
        handled_accounts = {row["account_id"] for row in bad_entries}
        for row in bad_entries:
            _add_problem(
                problems,
                "LEDGER_ENTRY_AMOUNT_MISMATCH",
                row["id"],
                f"transfer_amount={row['transfer_amount']}",
                f"entry_amount={row['entry_amount']}",
            )

        bad_transfers = conn.execute(
            """
            WITH totals AS (
              SELECT transfer_id,
                     COALESCE(sum(amount_paise) FILTER (WHERE direction = 'debit'), 0) AS debits,
                     COALESCE(sum(amount_paise) FILTER (WHERE direction = 'credit'), 0) AS credits
              FROM ledger_entries
              GROUP BY transfer_id
            )
            SELECT t.id, t.source_account_id, t.destination_account_id,
                   COALESCE(tot.debits, 0) AS debits,
                   COALESCE(tot.credits, 0) AS credits, t.amount_paise
            FROM transfers t
            LEFT JOIN totals tot ON tot.transfer_id = t.id
            WHERE t.status = 'posted'
              AND (
                COALESCE(tot.debits, 0) <> COALESCE(tot.credits, 0)
                OR COALESCE(tot.debits, 0) <> t.amount_paise
                OR COALESCE(tot.credits, 0) <> t.amount_paise
              )
            ORDER BY t.id
            """
        ).fetchall()
        counts["transfer_unbalanced"] = len(bad_transfers)
        new_bad_transfers = [row for row in bad_transfers if row["id"] not in handled_transfers]
        if new_bad_transfers:
            # Use the transfer's endpoints too: a deleted ledger leg would
            # otherwise hide one account from deduplication.
            handled_accounts.update(
                row["source_account_id"] for row in new_bad_transfers
            )
            handled_accounts.update(
                row["destination_account_id"] for row in new_bad_transfers
            )
        for row in new_bad_transfers:
            handled_transfers.add(row["id"])
            _add_problem(
                problems,
                "TRANSFER_UNBALANCED",
                row["id"],
                f"debits={row['debits']},credits={row['credits']},amount={row['amount_paise']}",
                "debits and credits differ",
            )

        if global_row["debits"] != global_row["credits"] and not problems:
            _add_problem(
                problems,
                "GLOBAL_UNBALANCED",
                None,
                str(global_row["debits"]),
                str(global_row["credits"]),
            )

        balance_rows = conn.execute(
            """
            WITH computed AS (
              SELECT account_id,
                     COALESCE(sum(amount_paise) FILTER (WHERE direction = 'credit'), 0)
                     - COALESCE(sum(amount_paise) FILTER (WHERE direction = 'debit'), 0) AS computed
              FROM ledger_entries
              GROUP BY account_id
            )
            SELECT a.id, a.balance_paise, COALESCE(c.computed, 0) AS computed
            FROM accounts a
            LEFT JOIN computed c ON c.account_id = a.id
            WHERE a.balance_paise <> COALESCE(c.computed, 0)
            ORDER BY a.id
            """
        ).fetchall()
        counts["stored_balance_mismatches"] = len(balance_rows)
        for row in balance_rows:
            if row["id"] in handled_accounts:
                continue
            _add_problem(
                problems,
                "ACCOUNT_BALANCE_MISMATCH",
                row["id"],
                f"computed={row['computed']}",
                f"stored={row['balance_paise']}",
            )

        overdrafts = conn.execute(
            """
            WITH computed AS (
              SELECT account_id,
                     COALESCE(sum(amount_paise) FILTER (WHERE direction = 'credit'), 0)
                     - COALESCE(sum(amount_paise) FILTER (WHERE direction = 'debit'), 0) AS computed
              FROM ledger_entries
              GROUP BY account_id
            )
            SELECT a.id, COALESCE(c.computed, 0) AS computed
            FROM accounts a
            LEFT JOIN computed c ON c.account_id = a.id
            WHERE a.owner_user_id IS NOT NULL AND COALESCE(c.computed, 0) < 0
            ORDER BY a.id
            """
        ).fetchall()
        counts["overdrafts"] = len(overdrafts)
        for row in overdrafts:
            if row["id"] in handled_accounts:
                continue
            _add_problem(
                problems,
                "USER_WALLET_OVERDRAFT",
                row["id"],
                "balance>=0",
                f"computed={row['computed']}",
            )

        orphan_transfers = conn.execute(
            """
            SELECT t.id
            FROM transfers t
            LEFT JOIN ledger_entries le ON le.transfer_id = t.id
            WHERE t.status = 'posted' AND le.id IS NULL
            ORDER BY t.id
            """
        ).fetchall()
        counts["orphan_transfers"] = len(orphan_transfers)
        for row in orphan_transfers:
            if row["id"] in handled_transfers:
                continue
            _add_problem(
                problems,
                "ORPHAN_TRANSFER",
                row["id"],
                "at least two ledger entries",
                "no ledger entries",
            )

        orphan_entries = conn.execute(
            """
            SELECT le.id
            FROM ledger_entries le
            LEFT JOIN transfers t ON t.id = le.transfer_id
            WHERE t.id IS NULL
            """
        ).fetchall()
        counts["orphan_entries"] = len(orphan_entries)
        for row in orphan_entries:
            _add_problem(problems, "ORPHAN_LEDGER_ENTRY", row["id"], "parent transfer", "missing")

        counts["problems_recorded"] = len(problems)
        if problems:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO reconciliation_problems(run_id, check_name, entity_id, expected, actual)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    [
                        (run_id, p["check_name"], p["entity_id"], p["expected"], p["actual"])
                        for p in problems
                    ],
                )
        status = "failed" if problems else "passed"
        conn.execute(
            """
            UPDATE reconciliation_runs
            SET ended_at = now(), status = %s, counts = %s::jsonb
            WHERE id = %s
            """,
            (status, json.dumps(counts), run_id),
        )

    explained = explain_recent_flags_in_new_transaction()
    return {
        "skipped": False,
        "run_id": run_id,
        "status": status,
        "problems": problems,
        "counts": counts,
        "flags_explained": explained,
        "triggered_by": triggered_by,
    }


def explain_recent_flags_in_new_transaction() -> int:
    client = get_client()
    with transaction() as conn:
        return explain_recent_flags(conn, client)
