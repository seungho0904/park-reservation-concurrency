"""Exercise a freshly seeded, disposable Compose stack through its live HTTP API."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.db import connect

API_URL = "http://127.0.0.1:8000"


def request(path: str, body: dict | None = None) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    http_request = Request(
        API_URL + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(http_request, timeout=20) as response:
            return response.status, json.load(response)
    except HTTPError as response:
        return response.code, json.load(response)


def main() -> None:
    if os.environ.get("SMOKE_TEST") != "1":
        raise RuntimeError("Run only against a disposable seeded database with SMOKE_TEST=1.")

    with connect() as connection:
        if connection.execute("SELECT count(*) AS total FROM reservations").fetchone()["total"]:
            raise RuntimeError("The smoke test requires a database with no reservations.")
        registration = {
            "username": "smoke_visitor",
            "display_name": "Smoke Visitor",
            "password": "Disposable smoke test password!",
        }
        status, user = request("/auth/register", registration)
        assert status == 201 and set(user) == {"id", "username", "display_name"}, user
        status, duplicate_user = request("/auth/register", registration)
        assert status == 409 and duplicate_user["detail"]["code"] == "username_taken"
        slots = connection.execute(
            "SELECT * FROM reservation_slots WHERE park_id = 1 ORDER BY visit_date, starts_at"
        ).fetchall()
        first_slot, second_slot = slots[:2]
        connection.execute(
            "UPDATE reservation_slots SET vehicle_capacity = 1 WHERE id = %s",
            (first_slot["id"],),
        )

        status, availability = request(f"/parks/1/slots?date={first_slot['visit_date']}")
        assert status == 200 and len(availability) == 3, availability
        assert availability[0]["remaining_capacity"] == 1, availability

        barrier = Barrier(20)

        def book(user_id: int):
            barrier.wait(timeout=15)
            return request(
                "/reservations",
                {
                    "user_id": user_id,
                    "slot_id": first_slot["id"],
                    "passenger_count": 4,
                    "infant_count": 1,
                },
            )

        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(book, range(1, 21)))
        assert [status for status, _ in results].count(201) == 1, results
        assert [status for status, _ in results].count(409) == 19, results
        assert all(
            body["detail"]["code"] == "slot_full" for status, body in results if status == 409
        ), results
        assert (
            connection.execute(
                "SELECT count(*) AS total FROM reservations WHERE slot_id = %s",
                (first_slot["id"],),
            ).fetchone()["total"]
            == 1
        )

        winner = next(body for status, body in results if status == 201)
        status, bookings = request(f"/users/{winner['user_id']}/reservations")
        assert status == 200 and len(bookings) == 1, bookings
        assert bookings[0]["id"] == winner["id"], bookings

        status, duplicate = request(
            "/reservations",
            {
                "user_id": winner["user_id"],
                "slot_id": second_slot["id"],
                "passenger_count": 4,
                "infant_count": 0,
            },
        )
        assert status == 409 and duplicate["detail"]["code"] == "daily_reservation_exists"
        status, availability = request(f"/parks/1/slots?date={first_slot['visit_date']}")
        assert status == 200 and availability[0]["remaining_capacity"] == 0, availability

    print("Live HTTP smoke test passed: 1 created, 19 sold out, 1 stored reservation.")
    print("Verified availability, reservation list, and duplicate daily booking rejection.")
    print("Verified registration and duplicate username rejection without returning credentials.")


if __name__ == "__main__":
    main()
