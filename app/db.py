import os
from contextlib import contextmanager
from typing import Iterator

from psycopg import IsolationLevel
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://wallet:wallet@localhost:5432/wallet")

pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=2,
    max_size=20,
    kwargs={"row_factory": dict_row},
    open=False,
)


@contextmanager
def transaction(isolation_level: IsolationLevel | None = None) -> Iterator:
    """Yield a connection with an explicit transaction.

    psycopg rolls the transaction back automatically when the body raises.
    """
    with pool.connection() as conn:
        if isolation_level is not None:
            conn.isolation_level = isolation_level
        with conn.transaction():
            yield conn


@contextmanager
def connection() -> Iterator:
    with pool.connection() as conn:
        yield conn


def reset_test_data(conn) -> None:
    """Truncate mutable data while keeping seeded accounts and job locks.

    Used by tests and demo helpers only. Never exposed through the API.
    """
    conn.execute(
        """
        TRUNCATE TABLE
          reconciliation_problems,
          reconciliation_runs,
          transfer_status_audit,
          risk_flags,
          idempotency_keys,
          ledger_entries,
          transfers,
          accounts,
          users,
          risk_rules,
          job_locks
        RESTART IDENTITY CASCADE
        """
    )
    conn.execute(
        """
        INSERT INTO accounts(kind, label, balance_paise) VALUES
          ('system_cash_in', 'external cash-in', 0),
          ('system_cash_out', 'external cash-out', 0),
          ('system_fee_revenue', 'fee revenue', 0)
        """
    )
    conn.execute("INSERT INTO job_locks(name) VALUES ('reconciliation')")
    conn.execute(
        """
        INSERT INTO risk_rules(code, description, rule_kind, threshold_paise, window_minutes, action) VALUES
          ('LARGE_TRANSFER', 'Hold a single transfer at or above the configured amount', 'amount', 500000, 0, 'hold'),
          ('VELOCITY_10M', 'Hold when one source account starts too many transfers in the window', 'velocity', 1000, 10, 'hold'),
          ('DAILY_OUTFLOW', 'Flag when one source account sends too much in the window', 'outflow', 2000000, 1440, 'flag')
        """
    )
