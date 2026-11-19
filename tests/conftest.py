"""Integration tests run against a separate `companies_test` database on the `just up` Postgres.

They're skipped (not failed) when Postgres isn't running, so `just test` works anywhere.
"""

import os

import psycopg
import pytest

from app.embedding import HashingEmbedder
from app.store import connect

PORT = os.environ.get("POSTGRES_PORT", "5432")
ADMIN_URL = f"postgresql://postgres:postgres@127.0.0.1:{PORT}/postgres"
TEST_URL = os.environ.get("TEST_DATABASE_URL", f"postgresql://postgres:postgres@127.0.0.1:{PORT}/companies_test")


@pytest.fixture(scope="session")
def database_url() -> str:
    try:
        with psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=2) as admin:
            exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = 'companies_test'").fetchone()
            if not exists:
                admin.execute("CREATE DATABASE companies_test")
    except psycopg.OperationalError:
        pytest.skip("Postgres isn't running (start it with `just up`)")
    return TEST_URL


@pytest.fixture
def conn(database_url):
    connection = connect(database_url)
    connection.execute("TRUNCATE company_events, rejected_messages, companies")
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder()
