-- Dedicated schema for the TQNA scanner, kept separate from vp-fa-scanner's
-- tables in the same Supabase project (schema-per-scanner isolation).

create schema if not exists tqna;

create table if not exists tqna.topics (
    topic_id bigint primary key,
    category text not null,
    title text not null,
    last_posts_count integer not null default 1,
    updated_at timestamptz not null default now()
);

create index if not exists idx_tqna_topics_category on tqna.topics (category);

-- Required: Supabase's PostgREST API only exposes schemas listed in
-- Project Settings -> API -> "Exposed schemas". After running this file,
-- add "tqna" to that list (alongside "public") or the scanner's API calls
-- will fail with a schema-not-found error.

-- Optional but recommended: grant the same roles PostgREST uses access
-- to the new schema (mirrors default "public" grants).
grant usage on schema tqna to anon, authenticated, service_role;
grant all on all tables in schema tqna to service_role;
alter default privileges in schema tqna grant all on tables to service_role;
