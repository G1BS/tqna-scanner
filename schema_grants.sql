-- Run this AFTER schema.sql succeeds. Kept separate so a grants failure
-- (e.g. insufficient privilege) can't roll back the table creation.
grant usage on schema tqna to service_role;
grant all on all tables in schema tqna to service_role;
alter default privileges in schema tqna grant all on tables to service_role;
