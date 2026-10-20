from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.registration import password_hasher
from app.schemas import UserRegister

pytestmark = pytest.mark.integration


def registration(**changes):
    return {
        "username": "park_visitor",
        "display_name": "Park Visitor",
        "password": "A long demonstration password!",
        **changes,
    }


def test_registration_hashes_password_and_returns_only_public_fields(client, database):
    body = registration()
    response = client.post("/auth/register", json=body)
    assert response.status_code == 201
    user = response.json()
    assert set(user) == {"id", "username", "display_name"}
    assert user["username"] == body["username"]
    assert user["display_name"] == body["display_name"]
    row = database.execute("SELECT * FROM users WHERE id = %s", (user["id"],)).fetchone()
    assert row["password_hash"].startswith("$argon2id$")
    assert password_hasher.verify(body["password"], row["password_hash"])
    assert not password_hasher.verify("A different demonstration password!", row["password_hash"])
    assert body["password"] not in row["password_hash"]
    assert row["password_hash"] not in response.text
    assert body["password"] not in response.text


def test_same_password_gets_different_salted_hashes(client, database):
    users = [
        client.post("/auth/register", json=registration(username=username))
        for username in ("visitor_one", "visitor_two")
    ]
    assert all(response.status_code == 201 for response in users)
    hashes = [
        database.execute(
            "SELECT password_hash FROM users WHERE id = %s", (response.json()["id"],)
        ).fetchone()["password_hash"]
        for response in users
    ]
    assert hashes[0] != hashes[1]
    assert all(password_hasher.verify(registration()["password"], value) for value in hashes)


def test_duplicate_username_is_normalized_and_does_not_replace_original_user(client, database):
    first = client.post("/auth/register", json=registration(username="  Park_Visitor  "))
    assert first.status_code == 201
    assert first.json()["username"] == "park_visitor"
    second = client.post(
        "/auth/register", json=registration(password="A different demonstration password!")
    )
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "username_taken"
    rows = database.execute("SELECT * FROM users WHERE username = 'park_visitor'").fetchall()
    assert len(rows) == 1
    assert password_hasher.verify(registration()["password"], rows[0]["password_hash"])
    # A failed registration leaves the connection and database usable.
    assert (
        client.post("/auth/register", json=registration(username="another_visitor")).status_code
        == 201
    )


def test_two_concurrent_registrations_cannot_claim_the_same_username(database):
    barrier = Barrier(2)

    def submit(username):
        with TestClient(app) as client:
            barrier.wait(timeout=15)
            return client.post("/auth/register", json=registration(username=username))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ("Park_Visitor", "park_visitor")))
    assert sorted(response.status_code for response in results) == [201, 409]
    assert (
        database.execute(
            "SELECT count(*) AS total FROM users WHERE username = 'park_visitor'"
        ).fetchone()["total"]
        == 1
    )


@pytest.mark.parametrize("length", [15, 128])
def test_password_length_boundaries_are_accepted(client, length):
    assert (
        client.post("/auth/register", json=registration(password="x" * length)).status_code == 201
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"username": "ab"},
        {"username": "a" * 33},
        {"username": "1visitor"},
        {"username": "park visitor"},
        {"username": "visitor@example.com"},
        {"username": 123},
        {"display_name": "   "},
        {"display_name": "x" * 101},
        {"password": "x" * 14},
        {"password": "x" * 129},
        {"password": 123456789012345},
        {"password": None},
        {"password_hash": "$argon2id$client-supplied-value"},
        {"id": 1},
    ],
)
def test_invalid_registration_does_not_write_or_echo_password(client, database, changes):
    body = registration(**changes)
    response = client.post("/auth/register", json=body)
    assert response.status_code == 422
    assert database.execute("SELECT count(*) AS total FROM users").fetchone()["total"] == 25
    for error in response.json()["detail"]:
        assert set(error) <= {"type", "loc", "msg"}
    if isinstance(body["password"], str):
        assert body["password"] not in response.text


def test_missing_password_never_echoes_request_body(client):
    body = registration()
    del body["password"]
    response = client.post("/auth/register", json=body)
    assert response.status_code == 422
    assert all("input" not in error for error in response.json()["detail"])


def test_password_is_not_trimmed_and_is_hidden_in_model_representation(client, database):
    body = registration(password="  a password with spaces  ", display_name="  Park Visitor  ")
    assert body["password"] not in repr(UserRegister.model_validate(body))
    response = client.post("/auth/register", json=body)
    assert response.status_code == 201
    assert response.json()["display_name"] == "Park Visitor"
    value = database.execute(
        "SELECT password_hash FROM users WHERE id = %s", (response.json()["id"],)
    ).fetchone()["password_hash"]
    assert password_hasher.verify(body["password"], value)
    assert not password_hasher.verify(body["password"].strip(), value)


def test_registered_user_can_use_existing_demo_booking_flow(client):
    user = client.post("/auth/register", json=registration()).json()
    response = client.post(
        "/reservations",
        json={
            "user_id": user["id"],
            "slot_id": 1,
            "passenger_count": 4,
            "infant_count": 0,
        },
    )
    assert response.status_code == 201
    assert len(client.get(f"/users/{user['id']}/reservations").json()) == 1


def test_registration_migration_preserves_demo_users_and_reservations(client, database):
    assert (
        client.post(
            "/reservations",
            json={
                "user_id": 1,
                "slot_id": 1,
                "passenger_count": 4,
                "infant_count": 0,
            },
        ).status_code
        == 201
    )
    # Recreate the previous users layout in this test's isolated schema.
    database.execute("ALTER TABLE users DROP COLUMN username, DROP COLUMN password_hash")
    migration = Path(__file__).parents[1] / "database/migrations/001_user_registration.sql"
    database.execute(migration.read_text())
    assert database.execute("SELECT count(*) AS total FROM users").fetchone()["total"] == 25
    assert len(client.get("/users/1/reservations").json()) == 1
    assert database.execute(
        "SELECT username, password_hash FROM users WHERE id = 1"
    ).fetchone() == {"username": None, "password_hash": None}
    assert client.post("/auth/register", json=registration()).status_code == 201


def test_database_requires_credentials_together(database):
    with pytest.raises(psycopg.errors.CheckViolation):
        database.execute("INSERT INTO users (display_name, username) VALUES ('Visitor', 'visitor')")


def test_database_rejects_noncanonical_username(database):
    with pytest.raises(psycopg.errors.CheckViolation):
        database.execute(
            """INSERT INTO users (display_name, username, password_hash)
               VALUES ('Visitor', 'UPPERCASE', 'test-only-hash')"""
        )
