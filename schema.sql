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

-- Kill switch + daily alert budget tracking, single-row settings table.
-- paused: set true to stop the scanner instantly (no code/secrets touch).
-- daily_count / last_reset_date: rolling daily alert cap bookkeeping.
create table if not exists tqna.settings (
    id integer primary key default 1,
    paused boolean not null default false,
    daily_count integer not null default 0,
    last_reset_date date not null default current_date,
    updated_at timestamptz not null default now()
);
insert into tqna.settings (id, paused, daily_count, last_reset_date)
values (1, false, 0, current_date)
on conflict (id) do nothing;

-- Safe to run even if tqna.settings already existed from an earlier setup
-- (adds the new columns without touching existing data).
alter table tqna.settings add column if not exists daily_count integer not null default 0;
alter table tqna.settings add column if not exists last_reset_date date not null default current_date;

-- Run schema_grants.sql separately, AFTER this succeeds — grant failures
-- must not roll back the table creation above.

-- For the Telegram /scan command listener — tracks the last processed
-- Telegram update ID so a command is never re-processed.
alter table tqna.settings add column if not exists last_telegram_update_id bigint not null default 0;
