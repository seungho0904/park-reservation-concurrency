import psycopg
from fastapi import HTTPException

from app.schemas import ReservationCreate, ReservationOut

RESERVATION_DETAILS = """
    SELECT r.id, r.user_id, r.slot_id, r.park_id, r.visit_date,
           r.passenger_count, r.infant_count, r.created_at,
           p.name AS park_name, p.timezone AS park_timezone,
           s.starts_at, s.ends_at
    FROM reservations r
    JOIN reservation_slots s ON s.id = r.slot_id
    JOIN parks p ON p.id = r.park_id
"""


def create_reservation(
    connection: psycopg.Connection, request: ReservationCreate
) -> ReservationOut:
    try:
        with connection.transaction():
            if not connection.execute(
                "SELECT id FROM users WHERE id = %s", (request.user_id,)
            ).fetchone():
                raise HTTPException(404, detail={"code": "user_not_found"})

            # All booking writers lock this parent row before counting reservations.
            slot = connection.execute(
                "SELECT * FROM reservation_slots WHERE id = %s FOR UPDATE",
                (request.slot_id,),
            ).fetchone()
            if slot is None:
                raise HTTPException(404, detail={"code": "slot_not_found"})

            # This improves the error message; UNIQUE is still the final protection
            # when the same user races to reserve two different slots on this date.
            existing = connection.execute(
                """SELECT id FROM reservations
                   WHERE park_id = %s AND user_id = %s AND visit_date = %s""",
                (slot["park_id"], request.user_id, slot["visit_date"]),
            ).fetchone()
            if existing:
                raise HTTPException(409, detail={"code": "daily_reservation_exists"})

            # Deliberately a separate statement AFTER obtaining the lock:
            # READ COMMITTED now sees the preceding lock holder's committed insert.
            count = connection.execute(
                "SELECT count(*) AS total FROM reservations WHERE slot_id = %s",
                (request.slot_id,),
            ).fetchone()["total"]
            if count >= slot["vehicle_capacity"]:
                raise HTTPException(409, detail={"code": "slot_full"})

            reservation = connection.execute(
                """INSERT INTO reservations
                   (user_id, slot_id, park_id, visit_date, passenger_count, infant_count)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (
                    request.user_id,
                    slot["id"],
                    slot["park_id"],
                    slot["visit_date"],
                    request.passenger_count,
                    request.infant_count,
                ),
            ).fetchone()
            row = connection.execute(
                RESERVATION_DETAILS + " WHERE r.id = %s", (reservation["id"],)
            ).fetchone()
            result = ReservationOut.model_validate(row)
        # Exiting the transaction commits BEFORE the caller sends 201.
        return result
    except psycopg.errors.UniqueViolation as exc:
        # Catch outside the context: the failed transaction has already rolled back.
        if exc.diag.constraint_name == "one_reservation_per_day":
            raise HTTPException(409, detail={"code": "daily_reservation_exists"}) from exc
        raise
