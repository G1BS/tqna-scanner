create table if not exists tqna_topics (
    topic_id bigint primary key,
    category text not null,
    title text not null,
    last_posts_count integer not null default 1,
    updated_at timestamptz not null default now()
);

create index if not exists idx_tqna_topics_category on tqna_topics (category);
