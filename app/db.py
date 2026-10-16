import os
from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row


def connect(database_url: str | None = None) -> psycopg.Connection:
    """Each request gets its own connection; transactions are explicit in the service."""
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Set DATABASE_URL to a PostgreSQL connection string.")
    connection = psycopg.connect(
        url,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=5,
        application_name="park-reservations",
    )
    connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
    return connection


def get_connection() -> Iterator[psycopg.Connection]:
    with connect() as connection:
        yield connection
