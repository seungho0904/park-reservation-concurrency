"""Idempotent demo data: python -m app.seed. Does not erase reservations."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.db import connect


def seed() -> None:
    timezone = "Pacific/Honolulu"
    first_date = datetime.now(ZoneInfo(timezone)).date() + timedelta(days=1)
    with connect() as connection, connection.transaction():
        connection.execute(
            """INSERT INTO parks (id, name, timezone)
               VALUES (1, 'Haleakala National Park', %s) ON CONFLICT (id) DO NOTHING""",
            (timezone,),
        )
        for user_id in range(1, 21):
            connection.execute(
                """INSERT INTO users (id, display_name) VALUES (%s, %s)
                   ON CONFLICT (id) DO NOTHING""",
                (user_id, f"Demo User {user_id}"),
            )
        for offset in range(7):
            for hour in (3, 4, 5):
                connection.execute(
                    """INSERT INTO reservation_slots
                       (park_id, visit_date, starts_at, ends_at, vehicle_capacity)
                       VALUES (1, %s, %s, %s, 10)
                       ON CONFLICT (park_id, visit_date, starts_at, ends_at) DO NOTHING""",
                    (first_date + timedelta(days=offset), time(hour), time(hour + 1)),
                )
        # Explicit demo IDs must not collide with future generated IDs.
        for table in ("parks", "users"):
            connection.execute(
                "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                "COALESCE((SELECT max(id) FROM " + table + "), 1))",
                (table,),
            )
    print(f"Park 1; users 1-20; demo dates {first_date} through {first_date + timedelta(days=6)}")
    print(f"GET /parks/1/slots?date={first_date}")


if __name__ == "__main__":
    seed()
