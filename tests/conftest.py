import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.db import connect
from app.main import app


@pytest.fixture
def database(monkeypatch):
    base_url = os.environ.get("TEST_DATABASE_URL")
    if not base_url:
        raise pytest.UsageError(
            "Set TEST_DATABASE_URL to a dedicated PostgreSQL database, "
            "or run: docker compose --profile test run --rm tests"
        )
    # Real commits are essential for concurrency tests. Each test uses an isolated
    # schema instead of wrapping every request in one shared rollback transaction.
    schema = "test_" + uuid4().hex
    with psycopg.connect(base_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    database_url = make_conninfo(base_url, options=f"-c search_path={schema},public")
    monkeypatch.setenv("DATABASE_URL", database_url)
    try:
        with connect() as connection:
            connection.execute((Path(__file__).parents[1] / "database" / "schema.sql").read_text())
            connection.execute(
                """INSERT INTO parks (id, name, timezone) VALUES
                   (1, 'Haleakala', 'Pacific/Honolulu'),
                   (2, 'Another Park', 'Pacific/Honolulu')"""
            )
            for user_id in range(1, 26):
                connection.execute(
                    "INSERT INTO users (id, display_name) VALUES (%s, %s)",
                    (user_id, f"User {user_id}"),
                )
            connection.execute(
                """INSERT INTO reservation_slots
                   (id, park_id, visit_date, starts_at, ends_at, vehicle_capacity) VALUES
                   (1, 1, '2030-12-01', '03:00', '04:00', 1),
                   (2, 1, '2030-12-01', '04:00', '05:00', 10),
                   (3, 1, '2030-12-02', '03:00', '04:00', 10),
                   (4, 2, '2030-12-01', '03:00', '04:00', 10)"""
            )
            connection.execute(
                "SELECT setval(pg_get_serial_sequence('reservation_slots', 'id'), 4)"
            )
            connection.execute("SELECT setval(pg_get_serial_sequence('users', 'id'), 25)")
            yield connection
    finally:
        # Only this test's newly created schema is removed.
        with psycopg.connect(base_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def client(database):
    with TestClient(app) as client:
        yield client
