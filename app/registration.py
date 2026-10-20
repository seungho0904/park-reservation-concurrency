import psycopg
from fastapi import HTTPException
from pwdlib import PasswordHash

from app.schemas import UserOut, UserRegister

password_hasher = PasswordHash.recommended()


def register_user(connection: psycopg.Connection, request: UserRegister) -> UserOut:
    # Hash before opening the transaction so expensive work holds no database locks.
    # Argon2id generates a fresh salt; the encoded result includes its parameters.
    password_hash = password_hasher.hash(request.password.get_secret_value())
    try:
        with connection.transaction():
            row = connection.execute(
                """INSERT INTO users (username, display_name, password_hash)
                   VALUES (%s, %s, %s) RETURNING id, username, display_name""",
                (request.username, request.display_name, password_hash),
            ).fetchone()
            user = UserOut.model_validate(row)
        return user
    except psycopg.errors.UniqueViolation as exc:
        # The unique index handles concurrent sign-ups for the same username.
        if exc.diag.constraint_name == "users_username_unique":
            raise HTTPException(409, detail={"code": "username_taken"}) from exc
        raise
