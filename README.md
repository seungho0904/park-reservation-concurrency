# Park Reservation Concurrency Lab

[![Docker Compose CI](https://github.com/seungho0904/park-reservation-concurrency/actions/workflows/ci.yml/badge.svg)](https://github.com/seungho0904/park-reservation-concurrency/actions/workflows/ci.yml)

A small backend project inspired by the challenge of booking a popular national park during a trip to Hawaii.
The core engineering problem is preventing overbooking when multiple users compete for the same limited slot.

**Acceptance criterion:** given a slot with capacity for one vehicle and 20 concurrent requests from distinct users,
exactly one request returns `201 Created`, the other 19 return `409 Conflict`, and the database contains one reservation,
assuming the server and database operate normally.

## MVP scope

- One reservation represents one vehicle. Each slot has its own vehicle capacity.
- Each reservation includes **1–8 passengers aged two or older**, including the driver.
- Infants under two are counted separately and excluded from the eight-passenger limit.
- Infant counts have no business upper limit; PostgreSQL's `integer` storage limit still applies.
- A user may hold at most one reservation per park and park-local visit date.
- Date-based availability queries return multiple slots, remaining vehicle capacity, and sold-out slots.
- A user's reservation list includes past and upcoming bookings, ordered by visit date and start time.
- Parks, demonstration users, and slots are seeded in advance. Requests identify users by demonstration user ID.

Pricing, payments, authentication, guest sessions, cancellations, administrative APIs, frontend development,
and cloud deployment are outside the MVP.

The first extension adds account registration and password hashing. Login, sessions, and booking authorization
are not implemented yet: callers can still supply user IDs to the existing booking APIs.

This project does not reproduce Haleakala's actual reservation policies. Hour-long slots and passenger limits
are project-specific rules. User IDs identify demonstration users; they do not authenticate callers or establish
that two different IDs belong to different people.

## Quick start

Requires Docker Desktop or Docker Engine with Docker Compose.

```sh
docker compose up --build -d
docker compose exec api python -m app.seed
```

The seed command creates park `1`, users `1–20`, and three slots per day for seven days starting tomorrow
in Hawaii's local timezone. It prints a date to use for availability queries. Running it again on the same day
does not create duplicate slots or delete existing reservations.

- Interactive API documentation: [http://localhost:8000/docs](http://localhost:8000/docs)
- Database health check: [http://localhost:8000/health](http://localhost:8000/health)

Query a date's slots in the API documentation, then use a returned slot ID to create a reservation:

```json
{
  "user_id": 1,
  "slot_id": 1,
  "passenger_count": 4,
  "infant_count": 1
}
```

Slot ID `1` is illustrative; use an ID from the availability response. Only passenger counts are collected,
not passenger names or birth dates. Adding `park_id` or `visit_date` to the request returns `422`;
the server derives these values from the selected slot.

To stop the containers while preserving reservation data:

```sh
docker compose stop
```

The schema script runs **only when the database volume is first initialized**. Editing it does not update an existing
database. Subsequent schema changes require a separate migration.

## Account registration

`POST /auth/register` creates a user and returns `201` with only `id`, `username`, and `display_name`:

```json
{
  "username": "park_visitor",
  "display_name": "Park Visitor",
  "password": "A long example password!"
}
```

- Usernames are trimmed and lowercased, must be 3–32 ASCII characters, start with a letter,
  and contain only letters, digits, or underscores. A database unique constraint handles concurrent sign-ups.
- Display names are trimmed and must contain 1–100 characters.
- Passwords must contain 15–128 characters and are not trimmed or otherwise normalized.
- Passwords are hashed with Argon2id through `pwdlib`; its implementation generates and encodes a fresh salt.
  Plaintext passwords are not stored, and neither plaintext nor hashes are returned in API responses.
- Duplicate usernames return `409` with `{"detail":{"code":"username_taken"}}`.
  Invalid requests return `422` without echoing input values or request bodies.

Registration creates an account, but does not log the caller in or protect the existing reservation APIs.
Session/token design and authenticated access to bookings are the next development step.

### Upgrading an existing database

Fresh databases use the current `database/schema.sql` and do not need this migration.
For an existing database created before registration was added, apply the following migration **once**:

```sh
docker compose up -d db
docker compose exec -T db psql -U park -d park -v ON_ERROR_STOP=1 < database/migrations/001_user_registration.sql
docker compose up --build -d
```

The migration preserves existing user IDs and reservations. Legacy demonstration users retain null credentials;
registration does not assign them shared passwords or claim existing display names as login identities.

## API

- `POST /auth/register`: creates an account with an Argon2id password hash; returns public user fields after commit.
- `GET /parks/{park_id}/slots?date=YYYY-MM-DD`: slots, capacity, and remaining spaces for a park-local date;
  returns `[]` if no slots exist on that date.
- `POST /reservations`: creates a reservation and returns `201` only after the transaction commits.
- `GET /users/{user_id}/reservations`: lists the demonstration user's reservations; returns `[]` if there are none.
- `GET /health`: executes a database query and returns `{"status":"ok"}` on success.

Unknown parks, users, or slots return `404`. Invalid dates, passenger counts, or registration fields return `422`.
Sold-out slots return `409` with `{"detail":{"code":"slot_full"}}`.
Duplicate daily bookings return `409` with `{"detail":{"code":"daily_reservation_exists"}}`.
If a user already has a daily booking, the duplicate-booking error takes precedence over the sold-out error.

Availability is a snapshot, not a promise of a successful booking. Capacity is checked again during reservation creation.
Visit dates and slot times are local to the park; creation timestamps use PostgreSQL `timestamptz`.
Booking release schedules and checks that reject bookings after a slot starts are outside this MVP.

## Architecture and data model

```text
Swagger UI / HTTP client
        |
FastAPI (input validation and HTTP responses)
        |
Reservation service (transaction, slot lock, reservation insert)
        | psycopg / parameterized SQL
PostgreSQL (data, foreign keys, CHECK and UNIQUE constraints)
```

The application consists of one FastAPI service and one PostgreSQL database. Synchronous `def` handlers run
in FastAPI's thread pool, with a separate database connection per request. Database concurrency guarantees
come from transactions, locks, and constraints rather than Python's `async` syntax or in-memory locks.
The project uses direct SQL without an ORM or connection pool to keep its booking logic visible and its scope small.

- `parks`: ID, name, and IANA timezone.
- `users`: ID, display name, optional unique username, and optional password hash. Credentials must be present together.
- `reservation_slots`: ID, park foreign key, visit date, start and end times, and vehicle capacity.
- `reservations`: ID, user foreign key, slot ID, park ID, visit date, passenger and infant counts, and creation timestamp.

A park has many slots, a slot has many reservations, and a user can reserve different dates.
A composite foreign key on `(slot_id, park_id, visit_date)` ensures that each reservation's park and date match its slot.
`UNIQUE (park_id, user_id, visit_date)` prevents duplicate daily reservations.
`CHECK` constraints enforce passenger ranges and positive capacity. An index on reservation `slot_id` supports counting
reservations for a slot.

## Concurrency design

A naive `SELECT reservation count → INSERT if space remains` can let two requests both observe zero bookings
and insert a reservation. Wrapping those operations in a transaction at the default isolation level is insufficient.

The implementation uses PostgreSQL **READ COMMITTED** with the following sequence:

1. Begin a transaction.
2. Lock the selected slot row with `SELECT ... FOR UPDATE`.
3. Check whether the user already has a reservation for that park and date.
4. Count the slot's reservations in a **separate statement after acquiring the lock**.
5. Insert a reservation if capacity remains.
6. Commit before returning `201`; roll back and release locks on failure.

Requests for the same slot wait for the preceding transaction to finish. Counting in a new statement after obtaining
the lock sees the preceding transaction's committed reservation. Combining locking and counting into one statement
would not provide the same fresh statement snapshot.

Slot locks alone cannot prevent a user from concurrently booking two different slots on the same date.
The daily `UNIQUE` constraint provides that guarantee. Unique-constraint errors are caught outside the transaction
context, after rollback, and translated into `409` responses.

### Alternatives considered

- **Transactions alone:** provide all-or-nothing changes, but do not prevent this check-then-insert race at READ COMMITTED.
- **Row locking and reservation counting:** the chosen approach; straightforward ordering without a separate booking counter.
- **Conditional atomic updates:** incrementing a counter only when `reserved_count < capacity` is also valid.
  The update acquires a row lock, and the reservation insert must share the same transaction. This adds a counter
  that must remain consistent with reservation records, so the MVP avoids that duplicated state.
- **Database constraints:** enforce daily uniqueness, input ranges, and referential integrity. An ordinary `CHECK`
  constraint cannot enforce an aggregate count across other reservation rows.
- **SERIALIZABLE isolation:** another option, but requires retrying entire transactions after serialization failures.
  The initial implementation uses explicit locking instead.

Capacity protection depends on **every reservation writer following the slot-locking protocol**.
Direct SQL inserts or future write paths must follow the same protocol. The design does not promise arrival-order
fairness, a particular throughput, or successful requests during infrastructure failures.

## Integration tests

```sh
docker compose --profile test run --build --rm tests
```

Tests use a dedicated temporary PostgreSQL database, not SQLite or database mocks.
Each test creates an isolated schema, performs real commits, and removes only its own schema afterward.
HTTP requests run through FastAPI's TestClient, with independent PostgreSQL connections per request.

Covered scenarios:

- Capacity 1, 20 concurrent users: one `201`, nineteen `409` responses, and one stored reservation.
- Capacity 5, 20 concurrent users: five successes and fifteen sold-out responses.
- One user concurrently reserving the same slot: one success.
- One user concurrently reserving different slots on the same date: one success.
- An uncommitted competing reservation forces a unique-constraint conflict: the losing request returns `409`,
  rolls back, and leaves its original slot available to another user.
- Bookings on other dates or at other parks, reservation-list ordering, and sold-out availability responses.
- Input validation, unknown IDs, and direct database checks for CHECK, UNIQUE, and composite foreign-key constraints.
- Repeatable seeding without duplicate slots or loss of existing reservations.
- Registration, salted password hashing and verification, password length boundaries, duplicate and concurrent
  usernames, validation responses without credentials, and migration of the previous user schema without data loss.

The main capacity test holds a slot lock before starting competing requests. It observes at least two blocked requests
in `pg_stat_activity` before releasing the lock, demonstrating actual database contention rather than sequential calls.
These are correctness tests, not throughput benchmarks or assertions about which user wins.

Local validation recorded on October 15, 2026: **31 tests passed** against temporary PostgreSQL 16.2;
Ruff lint and formatting checks also passed. TestClient emitted one Starlette deprecation warning
about its use of `httpx`; there were no test failures.

The registration extension was validated on October 20, 2026: **57 tests passed** on temporary PostgreSQL 16.2,
including the original 31 tests and 26 registration/migration cases. Ruff checks passed; the same TestClient
deprecation warning remains.

The [Docker Compose CI workflow](https://github.com/seungho0904/park-reservation-concurrency/actions/workflows/ci.yml)
builds the Docker images, starts PostgreSQL 18 and the API, waits for database-backed API readiness, and runs the
integration suite and style checks. It also seeds a disposable database and sends 20 concurrent requests to the live
HTTP server, asserting one successful reservation, nineteen sold-out responses, and one database record.
The smoke test checks availability, the winner's reservation list, rejection of another booking on the same date,
account registration, and rejection of duplicate usernames.
CI runs on pushes to `main` and pull requests, without requiring Docker on the developer's computer.

To test with local Python and a separate PostgreSQL test database:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
export TEST_DATABASE_URL='postgresql://park:park@localhost:5433/park_test'
.venv/bin/python -m pytest -q
.venv/bin/ruff check app tests
.venv/bin/ruff format --check app tests
```

## References

- [PostgreSQL row locking](https://www.postgresql.org/docs/current/explicit-locking.html)
- [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html)
- [PostgreSQL constraints](https://www.postgresql.org/docs/current/ddl-constraints.html)
- [FastAPI concurrency and synchronous handlers](https://fastapi.tiangolo.com/async/)
