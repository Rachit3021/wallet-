"""Show the transfer row lock with two database sessions.

Session A locks one wallet row. Session B tries the same SELECT ... FOR UPDATE
with a short lock_timeout, fails visibly, then succeeds after A commits.
"""

from app.db import connection, pool


def main() -> None:
    if pool.closed:
        pool.open()

    with connection() as setup:
        wallet_id = setup.execute(
            """
            SELECT id FROM accounts
            WHERE owner_user_id IS NOT NULL
            ORDER BY id LIMIT 1
            """
        ).fetchone()["id"]
        before = setup.execute(
            "SELECT balance_paise FROM accounts WHERE id = %s", (wallet_id,)
        ).fetchone()["balance_paise"]

    print(f"wallet id = {wallet_id}, initial balance = {before} paise")
    print("A: BEGIN; SELECT ... FROM accounts WHERE id = %s FOR UPDATE;")

    with pool.connection() as a, pool.connection() as b:
        a.execute("BEGIN")
        locked = a.execute(
            "SELECT balance_paise FROM accounts WHERE id = %s FOR UPDATE", (wallet_id,)
        ).fetchone()
        print(f"A: locked row, sees {locked['balance_paise']} paise")

        print("B: SET LOCAL lock_timeout = '1s'; SELECT ... FOR UPDATE;")
        b.execute("SET LOCAL lock_timeout = '1s'")
        try:
            b.execute(
                "SELECT balance_paise FROM accounts WHERE id = %s FOR UPDATE", (wallet_id,)
            )
            print("B: unexpectedly acquired the lock")
        except Exception as exc:
            print(f"B: {type(exc).__name__}: {str(exc).splitlines()[0]}")
        b.execute("ROLLBACK")

        print("A: UPDATE accounts SET balance_paise = balance_paise + 1 WHERE id = %s; COMMIT;")
        a.execute(
            "UPDATE accounts SET balance_paise = balance_paise + 1 WHERE id = %s", (wallet_id,)
        )
        a.execute("COMMIT")
        print("A: committed")

        print("B: SELECT ... FOR UPDATE;  -- retry after A commits")
        b.execute("BEGIN")
        after = b.execute(
            "SELECT balance_paise FROM accounts WHERE id = %s FOR UPDATE", (wallet_id,)
        ).fetchone()
        b.execute("ROLLBACK")
        print(f"B: now sees {after['balance_paise']} paise")

    with connection() as cleanup:
        cleanup.execute(
            "UPDATE accounts SET balance_paise = balance_paise - 1 WHERE id = %s", (wallet_id,)
        )
    print("cleanup: restored the original balance")


if __name__ == "__main__":
    main()
