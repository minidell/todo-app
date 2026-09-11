-- Runs once, only on an empty pgdata volume (postgres entrypoint convention).
-- Creates a second database used exclusively by the Postgres pytest lane
-- (TEST_DATABASE_URL points here so tests never touch the dev database).
CREATE DATABASE todo_test;
