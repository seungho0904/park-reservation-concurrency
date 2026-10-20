-- Apply once to a database created before registration was added.
-- Existing demonstration users and reservations are preserved.
BEGIN;

ALTER TABLE users
    ADD COLUMN username text,
    ADD COLUMN password_hash text,
    ADD CONSTRAINT users_username_unique UNIQUE (username),
    ADD CONSTRAINT users_username_format CHECK (username ~ '^[a-z][a-z0-9_]{2,31}$'),
    ADD CONSTRAINT users_credentials_pair
        CHECK ((username IS NULL) = (password_hash IS NULL));

COMMIT;
