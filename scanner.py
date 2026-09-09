"""
TradingQnA (tradingqna.com) FA/TA scanner.

Polls Discourse JSON API for selected categories, dedupes new topics/replies
against Supabase, filters out noise, scores + summarizes survivors with Groq
in one call, and sends a single color-coded Telegram digest per run.

Shares the same Supabase project as vp-fa-scanner (free-tier project limit),
but lives in its own Postgres schema ("tqna") for clean isolation. Telegram
bot, Groq key, and GitHub repo are still fully separate from vp-fa-scanner.

Kill switch: set tqna.settings.paused = true (one row, id=1) in Supabase to
stop the scanner from doing anything (no forum calls, no Groq, no Telegram)
without touching code or GitHub secrets. Flip it back to false to resume.
"""

import os
import re
import sys
import json
import time
import requests
from datetime import datetime, timezone
from supabase import create_client, ClientOptions

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://tradingqna.com"

# category_slug: (category_id, human label)
# All 7 categories — noise gets removed by the keyword + relevance filters
# below, not by dropping whole categories (F&O/Stocks/Daily Brief can carry
# real hypothesis-worthy signal just as much as the others).
CATEGORIES = {
    "fundamental-analysis": (15, "Fundamental Analysis"),
    "technical-analysis": (16, "Technical Analysis"),
    "futures-options": (19, "F&O"),
    "stocks": (13, "Stocks"),
    "algos-strategies-code": (6, "Algos, strategies, code"),
    "nifty-banknifty": (37, "Nifty & Bank Nifty"),
    "the-daily-brief": (54, "The Daily Brief"),
}

# Stage 1 cheap filter: skip topics whose title matches these patterns —
# broker/app support noise, not trading/investing content.
TITLE_DENYLIST = [
    r"\bnot working\b", r"\bapp crash", r"\bkyc\b", r"\botp\b",
    r"\blogin (issue|problem|error)", r"\bdemat\b.*\b(open|opening)\b",
    r"\bcustomer (care|support)\b", r"\bcomplaint\b", r"\brefund\b",
    r"\baccount (block|freeze|frozen)\b",
]
TITLE_DENYLIST_RE = re.compile("|".join(TITLE_DENYLIST), re.IGNORECASE)

# Stage 2: only include a NEW topic in the digest if Groq scores it >= this.
MIN_RELEVANCE_SCORE = 4

# Only ping about a known topic if it gained at least this many replies
# since the last scan (kills noise from single-reply bumps).
MIN_NEW_REPLIES_TO_ALERT = 3

# Realistic daily volume target: cap total alerts (new topics + active
# threads combined) per rolling UTC day, always keeping the highest-scoring
# items first. Prevents a big backlog (e.g. first-ever run) or a noisy day
# from flooding you — anything beyond the cap is simply not alerted (it's
# still recorded in tqna.topics so it won't be silently lost from dedupe).
DAILY_ALERT_CAP = 20

# Color-bar emoji by relevance score (5 = must-read, 4 = worth a look).
SCORE_COLOR = {5: "🟥", 4: "🟧"}

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")  # optional — falls back to raw excerpt if absent
TEST_LIMIT = os.environ.get("TEST_LIMIT")  # optional: cap topics scanned per category, for cheap manual tests
TEST_LIMIT = int(TEST_LIMIT) if TEST_LIMIT else None
# Optional: comma-separated topic IDs to force through extraction + digest
# regardless of dedupe/score, for testing the prompt on specific real posts
# without touching real dedupe history or the score threshold.
TEST_FORCE_TOPIC_IDS = set(
    int(x) for x in os.environ.get("TEST_FORCE_TOPIC_IDS", "").split(",") if x.strip()
)

SCHEMA_NAME = "tqna"
TOPICS_TABLE = "topics"
SETTINGS_TABLE = "settings"
HEADERS = {"User-Agent": "tqna-fa-scanner/1.0"}

TELEGRAM_MAX_LEN = 4000  # stay under Telegram's 4096-char hard limit

# Point the client at the "tqna" schema so this scanner's data lives
# completely separately from vp-fa-scanner's tables in the same project.
sb = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
    options=ClientOptions(schema=SCHEMA_NAME),
)


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

def is_paused() -> bool:
    """Check tqna.settings.paused. Defaults to False (running) if the row
    or table doesn't exist yet, so this never blocks a fresh setup."""
    try:
        res = sb.table(SETTINGS_TABLE).select("paused").eq("id", 1).execute()
        if res.data:
            return bool(res.data[0].get("paused", False))
    except Exception as e:
        print(f"Kill switch check failed (defaulting to running): {e}", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# Daily alert budget
# ---------------------------------------------------------------------------

def get_remaining_daily_budget() -> int:
    """Resets the counter at UTC midnight, returns how many more alerts can
    be sent today. Fails open (returns the full cap) if settings row/table
    is missing, so a DB hiccup never silently blocks all alerts."""
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        res = sb.table(SETTINGS_TABLE).select("*").eq("id", 1).execute()
        row = res.data[0] if res.data else {}
        last_reset = row.get("last_reset_date")
        daily_count = row.get("daily_count", 0) or 0

        if last_reset != today:
            daily_count = 0
            sb.table(SETTINGS_TABLE).update({
                "daily_count": 0, "last_reset_date": today,
            }).eq("id", 1).execute()

        return max(0, DAILY_ALERT_CAP - daily_count)
    except Exception as e:
        print(f"Daily budget check failed (defaulting to full cap): {e}", file=sys.stderr)
        return DAILY_ALERT_CAP


def record_alerts_sent(count: int):
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        res = sb.table(SETTINGS_TABLE).select("daily_count").eq("id", 1).execute()
        current = (res.data[0].get("daily_count", 0) or 0) if res.data else 0
        sb.table(SETTINGS_TABLE).update({
            "daily_count": current + count, "last_reset_date": today,
        }).eq("id", 1).execute()
    except Exception as e:
        print(f"Failed to record daily alert count: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Discourse API helpers
# ---------------------------------------------------------------------------

def fetch_category_topics(category_id: int):
    """Fetch latest topics for a category via Discourse JSON API."""
    url = f"{BASE_URL}/c/{category_id}.json"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json().get("topic_list", {}).get("topics", [])


def fetch_topic_thread(topic_id: int, max_posts: int = 8) -> str:
    """Fetch the first post plus up to max_posts-1 replies, so the LLM can
    see corrections/alternative views from other users, not just the OP."""
    url = f"{BASE_URL}/t/{topic_id}.json"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    posts = resp.json().get("post_stream", {}).get("posts", [])[:max_posts]
    if not posts:
        return ""

    parts = []
    for i, p in enumerate(posts):
        raw = p.get("cooked", "")
        text = re.sub("<[^<]+?>", "", raw).strip()
        if not text:
            continue
        role = "OP" if i == 0 else f"Reply {i}"
        parts.append(f"[{role}]: {text}")

    return "\n\n".join(parts)[:6000]


# ---------------------------------------------------------------------------
# Groq: combined relevance score + summary in a single call
# ---------------------------------------------------------------------------

def score_and_summarize(title: str, body: str, retries: int = 2):
    """
    Returns (score: int 1-5, extraction: dict).
    Score reflects how actionable/valuable the post is for a trading/investing
    audience (5 = high-conviction thesis, strategy, or analysis; 1 = noise).
    extraction is a structured analysis (subject, strategy, rules, risks,
    etc.) — not just a reworded summary of the raw text.
    Falls back to score=3 (neutral, included) with a minimal extraction if
    Groq is unavailable or fails, so a Groq outage never silently kills all
    alerts.
    """
    fallback = {"subject": "other", "main_idea": body[:300], "strategy": "",
                "rules": "", "key_points": "", "examples": "",
                "risks": "", "useful_replies": "", "terms": ""}

    if not GROQ_API_KEY or not body:
        return 3, fallback

    prompt = (
        "Read this TradingQnA post/thread and extract only the useful "
        "trading/investing knowledge.\n\n"
        "First identify the subject: investment, swing, intraday, F&O, "
        "options, futures, technical analysis, fundamental analysis, algo, "
        "backtesting, stocks, commodities, IPO, taxation, platform/tools, "
        "or other.\n\n"
        "Then concisely extract:\n"
        "- Main idea — what is being discussed?\n"
        "- Strategy/Method — how does it work?\n"
        "- Rules — entry, exit, conditions, filters, risk management, if applicable.\n"
        "- Key points — important and non-obvious insights.\n"
        "- Examples/Data — trades, numbers, backtests, performance claims.\n"
        "- Risks/Limitations — when it may fail or what to watch out for.\n"
        "- Useful replies — important corrections, additions, or alternative "
        "views from other users.\n"
        "- Important terms — stocks, indices, indicators, strategies, tools, etc.\n\n"
        "Ignore greetings, repetition, generic opinions, and irrelevant discussion. "
        "Do not invent information. Clearly distinguish facts from personal "
        "claims or opinions. Keep the output concise and information-dense. "
        "Use empty strings for any field with nothing genuinely useful to report.\n\n"
        "Also rate this post's value to an active trader/investor on a 1-5 "
        "scale (5 = high-conviction thesis/strategy/analysis worth reading "
        "now, 3 = generic/ok, 1 = noise/spam/off-topic with no real content).\n\n"
        f"Title: {title}\n\nThread:\n{body}\n\n"
        "Respond with ONLY one line of valid JSON, nothing else, no markdown "
        'fences: {"score": <int 1-5>, "subject": "<one of the categories above>", '
        '"main_idea": "<text, no newlines>", "strategy": "<text, no newlines>", '
        '"rules": "<text, no newlines>", "key_points": "<text, no newlines>", '
        '"examples": "<text, no newlines>", "risks": "<text, no newlines>", '
        '"useful_replies": "<text, no newlines>", "terms": "<comma-separated>"}'
    )

    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": "openai/gpt-oss-20b",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 900,
                    "temperature": 0.2,
                },
                timeout=30,
            )
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else 8 * (attempt + 1)
                print(f"Groq rate-limited, waiting {wait}s (attempt {attempt + 1})", file=sys.stderr)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            content = re.sub(r"^```(json)?|```$", "", content, flags=re.MULTILINE).strip()

            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                # Model likely got cut off — salvage at least the score,
                # keep the rest of the extraction empty rather than guessing.
                score_match = re.search(r'"score"\s*:\s*(\d)', content)
                score = int(score_match.group(1)) if score_match else 3
                return max(1, min(5, score)), fallback

            score = int(parsed.get("score", 3))
            extraction = {k: str(parsed.get(k, "")).strip() for k in
                          ("subject", "main_idea", "strategy", "rules", "key_points",
                           "examples", "risks", "useful_replies", "terms")}
            return max(1, min(5, score)), extraction

        except Exception as e:
            print(f"Groq extraction failed, including with neutral score: {e}", file=sys.stderr)
            return 3, fallback

    # Exhausted retries (all 429s)
    print("Groq rate limit persisted after retries, including with neutral score", file=sys.stderr)
    return 3, fallback


# ---------------------------------------------------------------------------
# Supabase dedupe state
# ---------------------------------------------------------------------------

def get_known_topic(topic_id: int):
    res = sb.table(TOPICS_TABLE).select("*").eq("topic_id", topic_id).execute()
    return res.data[0] if res.data else None


def upsert_topic(topic_id: int, category_slug: str, last_posts_count: int, title: str):
    sb.table(TOPICS_TABLE).upsert({
        "topic_id": topic_id,
        "category": category_slug,
        "title": title,
        "last_posts_count": last_posts_count,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"Telegram send failed: {resp.text}", file=sys.stderr)


def format_extraction_block(it: dict) -> str:
    """Render one topic's structured extraction as a compact Telegram block."""
    ex = it["extraction"]
    bar = SCORE_COLOR.get(it["score"], "🟨")
    lines = [
        f"{bar} <b>[{ex.get('subject', 'other')}] {it['label']}</b>",
        f"<a href='{it['url']}'>{it['title']}</a>",
    ]
    if ex.get("main_idea"):
        lines.append(f"<b>Idea:</b> {ex['main_idea']}")
    if ex.get("strategy"):
        lines.append(f"<b>Strategy:</b> {ex['strategy']}")
    if ex.get("rules"):
        lines.append(f"<b>Rules:</b> {ex['rules']}")
    if ex.get("key_points"):
        lines.append(f"<b>Key points:</b> {ex['key_points']}")
    if ex.get("examples"):
        lines.append(f"<b>Data:</b> {ex['examples']}")
    if ex.get("risks"):
        lines.append(f"<b>Risks:</b> {ex['risks']}")
    if ex.get("useful_replies"):
        lines.append(f"<b>Notable replies:</b> {ex['useful_replies']}")
    if ex.get("terms"):
        lines.append(f"<b>Terms:</b> {ex['terms']}")
    return "\n".join(lines)


def send_digest(new_items: list, reply_items: list):
    """Build and send one (or a few, if long) digest message(s) instead of
    one message per topic — sorted so the most important items lead."""
    if not new_items and not reply_items:
        return

    new_items.sort(key=lambda x: -x["score"])

    lines = [f"📊 <b>TQNA Digest</b> — {datetime.now(timezone.utc).strftime('%d %b %H:%M UTC')}\n"]

    if new_items:
        lines.append(f"<b>New topics ({len(new_items)})</b>")
        for it in new_items:
            lines.append(format_extraction_block(it))
        lines.append("")

    if reply_items:
        lines.append(f"<b>Active threads ({len(reply_items)})</b>")
        for it in reply_items:
            lines.append(
                f"💬 <b>{it['label']}</b> — +{it['new_replies']} replies — "
                f"<a href='{it['url']}'>{it['title']}</a>"
            )

    full_text = "\n\n".join(lines)

    # Split into multiple messages if we exceed Telegram's limit.
    chunks = []
    current = ""
    for block in full_text.split("\n\n"):
        if len(current) + len(block) + 2 > TELEGRAM_MAX_LEN:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)

    for chunk in chunks:
        send_telegram(chunk)
        time.sleep(1)


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def test_forced_topics(new_items: list):
    """Directly fetch and score specific topic IDs (via TEST_FORCE_TOPIC_IDS),
    bypassing category listings and dedupe — for testing the extraction
    prompt on real, chosen posts without touching real dedupe state."""
    for topic_id in TEST_FORCE_TOPIC_IDS:
        try:
            url = f"{BASE_URL}/t/{topic_id}.json"
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            title = data.get("title", f"Topic {topic_id}")
            slug = data.get("slug", str(topic_id))
            topic_url = f"{BASE_URL}/t/{slug}/{topic_id}"

            thread = fetch_topic_thread(topic_id)
            score, extraction = score_and_summarize(title, thread)
            time.sleep(3)

            new_items.append({
                "label": "TEST", "title": title, "url": topic_url,
                "score": score, "extraction": extraction,
            })
        except Exception as e:
            print(f"Forced test topic {topic_id} failed: {e}", file=sys.stderr)


def scan_category(slug: str, category_id: int, label: str, new_items: list, reply_items: list):
    topics = fetch_category_topics(category_id)
    if TEST_LIMIT:
        topics = topics[:TEST_LIMIT]
    for t in topics:
        topic_id = t["id"]
        title = t["title"]
        posts_count = t.get("posts_count", 1)
        topic_url = f"{BASE_URL}/t/{t['slug']}/{topic_id}"

        if TITLE_DENYLIST_RE.search(title):
            continue  # Stage 1: cheap keyword filter, skip entirely (no dedupe write)

        known = get_known_topic(topic_id)

        if known is None:
            thread = fetch_topic_thread(topic_id)
            score, extraction = score_and_summarize(title, thread)
            time.sleep(3)  # pace Groq calls to stay under free-tier rate limit
            upsert_topic(topic_id, slug, posts_count, title)  # record regardless of score

            if score >= MIN_RELEVANCE_SCORE:
                new_items.append({
                    "label": label, "title": title, "url": topic_url,
                    "score": score, "extraction": extraction,
                })

        elif posts_count > known["last_posts_count"]:
            new_replies = posts_count - known["last_posts_count"]
            upsert_topic(topic_id, slug, posts_count, title)

            if new_replies >= MIN_NEW_REPLIES_TO_ALERT:
                reply_items.append({
                    "label": label, "title": title, "url": topic_url,
                    "new_replies": new_replies,
                })

        time.sleep(1)  # be polite to the forum API


def main():
    if is_paused():
        print("Kill switch is ON (tqna.settings.paused = true) — skipping this run.")
        return

    new_items = []
    reply_items = []

    if TEST_FORCE_TOPIC_IDS:
        print(f"TEST MODE: forcing extraction on topic IDs {TEST_FORCE_TOPIC_IDS}, skipping normal category scan.")
        test_forced_topics(new_items)
    else:
        for slug, (category_id, label) in CATEGORIES.items():
            try:
                scan_category(slug, category_id, label, new_items, reply_items)
            except Exception as e:
                print(f"Error scanning {slug}: {e}", file=sys.stderr)
            time.sleep(2)

    new_items.sort(key=lambda x: -x["score"])
    reply_items.sort(key=lambda x: -x["new_replies"])

    budget = get_remaining_daily_budget()
    total_found = len(new_items) + len(reply_items)

    if budget <= 0:
        print(f"Daily alert cap ({DAILY_ALERT_CAP}) already reached — found "
              f"{total_found} qualifying items but sending none today.")
        return

    # Fill the budget with highest-priority new topics first, then reply pings.
    kept_new = new_items[:budget]
    remaining = budget - len(kept_new)
    kept_replies = reply_items[:remaining]

    if total_found > len(kept_new) + len(kept_replies):
        skipped = total_found - len(kept_new) - len(kept_replies)
        print(f"Daily cap trimmed {skipped} lower-priority item(s) — sending "
              f"top {len(kept_new)} new + {len(kept_replies)} thread alerts.")

    send_digest(kept_new, kept_replies)
    record_alerts_sent(len(kept_new) + len(kept_replies))
    print(f"Run complete: {len(kept_new)} high-value new topics, {len(kept_replies)} active threads alerted.")


if __name__ == "__main__":
    main()
