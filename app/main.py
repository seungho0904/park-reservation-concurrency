from datetime import date
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.db import get_connection
from app.registration import register_user
from app.reservations import RESERVATION_DETAILS, create_reservation
from app.schemas import ReservationCreate, ReservationOut, SlotOut, UserOut, UserRegister

app = FastAPI(
    title="Park Reservation Concurrency Lab",
    description=(
        "Learning MVP: one vehicle per booking, at most one booking per user/park/local date. "
        "Registration stores password hashes, but login and booking authorization are not yet "
        "implemented. Booking requests still supply their own user IDs. "
        "Slot times use the park's local timezone."
    ),
)

Database = Annotated[psycopg.Connection, Depends(get_connection)]
ResourceId = Annotated[int, Path(gt=0, le=9223372036854775807)]


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request, exc: RequestValidationError):
    # FastAPI's default validation response can echo raw input, including passwords.
    # Return only locations and error descriptions; never the request body or context.
    errors = [
        {key: error[key] for key in ("type", "loc", "msg") if key in error}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.post("/auth/register", response_model=UserOut, status_code=201)
def register(request: UserRegister, connection: Database):
    return register_user(connection, request)


@app.get("/health")
def health(connection: Database) -> dict[str, str]:
    connection.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/parks/{park_id}/slots", response_model=list[SlotOut])
def list_slots(
    park_id: ResourceId,
    connection: Database,
    visit_date: Annotated[date, Query(alias="date")],
):
    if not connection.execute("SELECT id FROM parks WHERE id = %s", (park_id,)).fetchone():
        raise HTTPException(404, detail={"code": "park_not_found"})
    return connection.execute(
        """SELECT s.id, s.park_id, s.visit_date, s.starts_at, s.ends_at,
                  s.vehicle_capacity,
                  s.vehicle_capacity - count(r.id) AS remaining_capacity
           FROM reservation_slots s
           LEFT JOIN reservations r ON r.slot_id = s.id
           WHERE s.park_id = %s AND s.visit_date = %s
           GROUP BY s.id
           ORDER BY s.starts_at, s.id""",
        (park_id, visit_date),
    ).fetchall()


@app.post("/reservations", response_model=ReservationOut, status_code=201)
def book(request: ReservationCreate, connection: Database):
    return create_reservation(connection, request)


@app.get("/users/{user_id}/reservations", response_model=list[ReservationOut])
def list_reservations(user_id: ResourceId, connection: Database):
    if not connection.execute("SELECT id FROM users WHERE id = %s", (user_id,)).fetchone():
        raise HTTPException(404, detail={"code": "user_not_found"})
    return connection.execute(
        RESERVATION_DETAILS + " WHERE r.user_id = %s ORDER BY r.visit_date, s.starts_at, r.id",
        (user_id,),
    ).fetchall()
