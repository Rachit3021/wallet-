"""Run scripts/corrupt.sql against the configured database."""

from pathlib import Path

from app.db import connection


def run() -> None:
    sql = Path(__file__).with_name("corrupt.sql").read_text(encoding="utf-8")
    with connection() as conn:
        conn.execute(sql)


if __name__ == "__main__":
    run()
    print("injected three reconciliation corruptions")
