from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import monotonic, sleep

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app
from app.seed import seed

pytestmark = pytest.mark.integration


def payload(user_id=1, slot_id=1, **changes):
    return {
        "user_id": user_id,
        "slot_id": slot_id,
        "passenger_count": 4,
        "infant_count": 1,
        **changes,
    }


def submit(barrier, user_id, slot_id=1):
    # Each concurrent HTTP request has its own client and database connection.
    with TestClient(app) as client:
        barrier.wait(timeout=15)
        return client.post("/reservations", json=payload(user_id, slot_id))


def wait_for_database_lock(query_fragment, minimum=1):
    """Observe actual PostgreSQL blocking instead of assuming a sleep caused a race."""
    deadline = monotonic() + 15
    with connect() as observer:
        while monotonic() < deadline:
            count = observer.execute(
                """SELECT count(*) AS total FROM pg_stat_activity
                   WHERE datname = current_database()
                     AND application_name = 'park-reservations'
                     AND wait_event_type = 'Lock'
                     AND query LIKE %s""",
                (f"%{query_fragment}%",),
            ).fetchone()["total"]
            if count >= minimum:
                return
            sleep(0.01)
    raise AssertionError(f"Expected {minimum} blocked requests for {query_fragment!r}")


def test_twenty_users_compete_for_last_vehicle_space(database):
    barrier = Barrier(20)
    with ThreadPoolExecutor(max_workers=20) as executor:
        with database.transaction():
            database.execute("SELECT id FROM reservation_slots WHERE id = 1 FOR UPDATE")
            futures = [executor.submit(submit, barrier, user_id) for user_id in range(1, 21)]
            # Hold the slot until at least two requests really contend in PostgreSQL.
            wait_for_database_lock("FROM reservation_slots", minimum=2)
        responses = [future.result(timeout=20) for future in futures]

    assert [r.status_code for r in responses].count(201) == 1
    assert [r.status_code for r in responses].count(409) == 19
    assert all(r.json()["detail"]["code"] == "slot_full" for r in responses if r.status_code == 409)
    assert (
        database.execute("SELECT count(*) AS total FROM reservations WHERE slot_id = 1").fetchone()[
            "total"
        ]
        == 1
    )


def test_capacity_greater_than_one(database):
    database.execute("UPDATE reservation_slots SET vehicle_capacity = 5 WHERE id = 1")
    barrier = Barrier(20)
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(submit, barrier, user_id) for user_id in range(1, 21)]
        responses = [future.result(timeout=20) for future in futures]
    assert [r.status_code for r in responses].count(201) == 5
    assert [r.status_code for r in responses].count(409) == 15
    assert database.execute("SELECT count(*) AS total FROM reservations").fetchone()["total"] == 5


def test_same_user_competes_for_two_slots_on_same_date(database):
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(submit, barrier, 1, slot_id) for slot_id in (1, 2)]
        responses = [future.result(timeout=20) for future in futures]
    assert sorted(r.status_code for r in responses) == [201, 409]
    failure = next(r for r in responses if r.status_code == 409)
    assert failure.json()["detail"]["code"] == "daily_reservation_exists"
    assert (
        database.execute("SELECT count(*) AS total FROM reservations WHERE user_id = 1").fetchone()[
            "total"
        ]
        == 1
    )


def test_unique_constraint_conflict_rolls_back_and_releases_slot(database, client):
    # The competing booking is uncommitted and invisible to the API's SELECT.
    # Its unique-index entry nevertheless forces the API's INSERT to wait.
    with ThreadPoolExecutor(max_workers=1) as executor:
        with database.transaction():
            database.execute(
                """INSERT INTO reservations
                   (user_id, slot_id, park_id, visit_date, passenger_count, infant_count)
                   VALUES (1, 2, 1, '2030-12-01', 4, 0)"""
            )
            future = executor.submit(client.post, "/reservations", json=payload())
            wait_for_database_lock("INSERT INTO reservations")
        response = future.result(timeout=20)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "daily_reservation_exists"
    # Losing the daily-booking race must not consume slot 1 or leave its lock held.
    assert client.post("/reservations", json=payload(user_id=2)).status_code == 201


def test_same_user_same_slot_concurrently(database):
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(submit, barrier, 1) for _ in range(2)]
        responses = [future.result(timeout=20) for future in futures]
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert database.execute("SELECT count(*) AS total FROM reservations").fetchone()["total"] == 1


def test_booking_and_my_reservations(client):
    response = client.post("/reservations", json=payload())
    assert response.status_code == 201
    booking = response.json()
    assert booking["passenger_count"] == 4
    assert booking["infant_count"] == 1
    assert booking["park_timezone"] == "Pacific/Honolulu"
    assert booking["visit_date"] == "2030-12-01"
    assert client.get("/users/1/reservations").json() == [booking]
    assert client.get("/users/2/reservations").json() == []


def test_slot_listing_includes_sold_out_and_remaining_spaces(client):
    assert client.post("/reservations", json=payload()).status_code == 201
    response = client.get("/parks/1/slots", params={"date": "2030-12-01"})
    assert response.status_code == 200
    assert [s["id"] for s in response.json()] == [1, 2]
    assert [s["remaining_capacity"] for s in response.json()] == [0, 10]
    assert client.get("/parks/1/slots?date=2030-12-03").json() == []
    assert client.post("/reservations", json=payload(user_id=2)).status_code == 409


def test_daily_limit_allows_other_dates_and_parks_and_sorts_list(client):
    for slot_id in (3, 1, 4):
        assert client.post("/reservations", json=payload(slot_id=slot_id)).status_code == 201
    duplicate = client.post("/reservations", json=payload(slot_id=2))
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "daily_reservation_exists"
    bookings = client.get("/users/1/reservations").json()
    assert len(bookings) == 3
    assert [booking["visit_date"] for booking in bookings] == [
        "2030-12-01",
        "2030-12-01",
        "2030-12-02",
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"passenger_count": 0},
        {"passenger_count": 9},
        {"infant_count": -1},
        {"passenger_count": True},
        {"passenger_count": 1.5},
        {"user_id": "1"},
        {"user_id": 0},
        {"user_id": 2**63},
        {"infant_count": 2**31},
        {"park_id": 2},
        {"visit_date": "2030-12-02"},
    ],
)
def test_invalid_payload_does_not_write(client, database, changes):
    response = client.post("/reservations", json=payload(**changes))
    assert response.status_code == 422
    assert database.execute("SELECT count(*) AS total FROM reservations").fetchone()["total"] == 0


def test_eight_passengers_and_infants_use_one_vehicle_space(client):
    response = client.post("/reservations", json=payload(passenger_count=8, infant_count=20))
    assert response.status_code == 201
    assert response.json()["infant_count"] == 20
    slots = client.get("/parks/1/slots?date=2030-12-01").json()
    assert slots[0]["remaining_capacity"] == 0


@pytest.mark.parametrize(
    "path",
    [
        "/users/999/reservations",
        "/parks/999/slots?date=2030-12-01",
    ],
)
def test_unknown_resources(client, path):
    assert client.get(path).status_code == 404


def test_unknown_booking_user_or_slot(client):
    assert client.post("/reservations", json=payload(user_id=999)).status_code == 404
    assert client.post("/reservations", json=payload(slot_id=999)).status_code == 404


def test_invalid_query_and_path(client):
    assert client.get("/parks/1/slots?date=not-a-date").status_code == 422
    assert client.get("/parks/1/slots").status_code == 422
    assert client.get("/users/0/reservations").status_code == 422


def test_database_rejects_mismatched_slot_date(database):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        database.execute(
            """INSERT INTO reservations
               (user_id, slot_id, park_id, visit_date, passenger_count, infant_count)
               VALUES (1, 1, 1, '2030-12-02', 4, 0)"""
        )


def test_database_rejects_duplicate_day(database):
    statement = """INSERT INTO reservations
                   (user_id, slot_id, park_id, visit_date, passenger_count, infant_count)
                   VALUES (1, %s, 1, '2030-12-01', 4, 0)"""
    database.execute(statement, (1,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        database.execute(statement, (2,))


@pytest.mark.parametrize("passengers,infants", [(0, 0), (9, 0), (1, -1)])
def test_database_checks_passenger_counts(database, passengers, infants):
    with pytest.raises(psycopg.errors.CheckViolation):
        database.execute(
            """INSERT INTO reservations
               (user_id, slot_id, park_id, visit_date, passenger_count, infant_count)
               VALUES (1, 1, 1, '2030-12-01', %s, %s)""",
            (passengers, infants),
        )


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_demo_seed_is_repeatable_and_preserves_reservations(client, database):
    assert client.post("/reservations", json=payload()).status_code == 201
    seed()
    slots_after_first_seed = database.execute(
        "SELECT count(*) AS total FROM reservation_slots"
    ).fetchone()["total"]
    seed()
    assert (
        database.execute("SELECT count(*) AS total FROM reservation_slots").fetchone()["total"]
        == slots_after_first_seed
    )
    assert len(client.get("/users/1/reservations").json()) == 1
