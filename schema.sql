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

-- Kill switch: a single-row settings table. Set paused = true to stop the
-- scanner immediately (no forum/Groq/Telegram calls) without touching code
-- or GitHub secrets. Flip back to false to resume.
create table if not exists tqna.settings (
    id integer primary key default 1,
    paused boolean not null default false,
    updated_at timestamptz not null default now()
);
insert into tqna.settings (id, paused) values (1, false)
on conflict (id) do nothing;

-- Run these two statements separately (in their own query) after confirming
-- the tables above were created successfully — grant failures must not roll
-- back the table creation above.
