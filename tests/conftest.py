import os

os.environ.setdefault("LLM_PROVIDER", "mock")

import pytest
from fastapi.testclient import TestClient

from app.db import connection, pool, reset_test_data
from app.main import app


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def clean(client):
    del client  # keep the client fixture alive so the pool is open
    with connection() as conn:
        reset_test_data(conn)
