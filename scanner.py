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
# Trimmed to the categories most likely to carry actionable trading/investing
# signal. "F&O", "Stocks", and "The Daily Brief" were dropped — highest
# volume, lowest signal (mostly broker/support chatter or generic news).
CATEGORIES = {
    "fundamental-analysis": (15, "Fundamental Analysis"),
    "technical-analysis": (16, "Technical Analysis"),
    "algos-strategies-code": (6, "Algos, strategies, code"),
    "nifty-banknifty": (37, "Nifty & Bank Nifty"),
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

# Color-bar emoji by relevance score (5 = must-read, 4 = worth a look).
SCORE_COLOR = {5: "🟥", 4: "🟧"}

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")  # optional — falls back to raw excerpt if absent

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
# Discourse API helpers
# ---------------------------------------------------------------------------

def fetch_category_topics(category_id: int):
    """Fetch latest topics for a category via Discourse JSON API."""
    url = f"{BASE_URL}/c/{category_id}.json"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json().get("topic_list", {}).get("topics", [])


def fetch_topic_excerpt(topic_id: int) -> str:
    """Fetch the first post of a topic for a scoring/summary source."""
    url = f"{BASE_URL}/t/{topic_id}.json"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    posts = resp.json().get("post_stream", {}).get("posts", [])
    if not posts:
        return ""
    raw = posts[0].get("cooked", "")
    text = re.sub("<[^<]+?>", "", raw)  # crude tag strip
    return text.strip()[:1500]


# ---------------------------------------------------------------------------
# Groq: combined relevance score + summary in a single call
# ---------------------------------------------------------------------------

def score_and_summarize(title: str, body: str):
    """
    Returns (score: int 1-5, summary: str).
    Score reflects how actionable/valuable the post is for a trading/investing
    audience (5 = high-conviction thesis, strategy, or analysis; 1 = noise).
    Falls back to score=3 (neutral, included) and raw excerpt if Groq is
    unavailable or fails, so a Groq outage never silently kills all alerts.
    """
    if not GROQ_API_KEY or not body:
        return 3, body[:300]

    prompt = (
        "You are filtering posts for a trading/investing alert feed. "
        "Rate this forum post's value to an active trader/investor on a "
        "1-5 scale (5 = high-conviction thesis/strategy/analysis worth "
        "reading now, 3 = generic/ok, 1 = noise/spam/off-topic). "
        "Then give a 2-3 sentence plain-language summary.\n\n"
        f"Title: {title}\n\nPost:\n{body}\n\n"
        'Respond ONLY as JSON: {"score": <int 1-5>, "summary": "<text>"}'
    )
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": "openai/gpt-oss-20b",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 250,
                "temperature": 0.2,
            },
            timeout=20,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(content)
        score = int(parsed.get("score", 3))
        summary = str(parsed.get("summary", body[:300])).strip()
        return max(1, min(5, score)), summary
    except Exception as e:
        print(f"Groq scoring failed, including with neutral score: {e}", file=sys.stderr)
        return 3, body[:300]


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
            bar = SCORE_COLOR.get(it["score"], "🟨")
            lines.append(
                f"{bar} <b>{it['label']}</b> — <a href='{it['url']}'>{it['title']}</a>\n"
                f"{it['summary']}"
            )
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

def scan_category(slug: str, category_id: int, label: str, new_items: list, reply_items: list):
    topics = fetch_category_topics(category_id)
    for t in topics:
        topic_id = t["id"]
        title = t["title"]
        posts_count = t.get("posts_count", 1)
        topic_url = f"{BASE_URL}/t/{t['slug']}/{topic_id}"

        if TITLE_DENYLIST_RE.search(title):
            continue  # Stage 1: cheap keyword filter, skip entirely (no dedupe write)

        known = get_known_topic(topic_id)

        if known is None:
            excerpt = fetch_topic_excerpt(topic_id)
            score, summary = score_and_summarize(title, excerpt)
            upsert_topic(topic_id, slug, posts_count, title)  # record regardless of score

            if score >= MIN_RELEVANCE_SCORE:
                new_items.append({
                    "label": label, "title": title, "url": topic_url,
                    "score": score, "summary": summary,
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

    for slug, (category_id, label) in CATEGORIES.items():
        try:
            scan_category(slug, category_id, label, new_items, reply_items)
        except Exception as e:
            print(f"Error scanning {slug}: {e}", file=sys.stderr)
        time.sleep(2)

    send_digest(new_items, reply_items)
    print(f"Run complete: {len(new_items)} high-value new topics, {len(reply_items)} active threads alerted.")


if __name__ == "__main__":
    main()
