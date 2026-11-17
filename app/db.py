import os
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
pool = ConnectionPool(
    conninfo=os.environ["DATABASE_URL"],
    min_size=2,
    max_size=20,           # matters later for the stampede test
    kwargs={"row_factory": dict_row},
    open=False,
)
