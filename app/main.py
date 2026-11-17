from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.db import pool
@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open()      # runs when the app starts
    yield
    pool.close()     # runs when the app stops
app = FastAPI(title="Wallet", lifespan=lifespan)
@app.get("/health")
def health():
    with pool.connection() as conn:
        row = conn.execute("SELECT version() AS v, now() AS t").fetchone()
    return {"status": "ok", "db": row["v"], "db_time": str(row["t"])}
