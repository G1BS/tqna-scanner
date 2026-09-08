"""
TradingQnA (tradingqna.com) FA/TA scanner.
Polls Discourse JSON API for selected categories, dedupes new topics/replies
against Supabase, optionally summarizes with Groq, and sends Telegram alerts.

Shares the same Supabase project as vp-fa-scanner (free-tier project limit),
but lives in its own Postgres schema ("tqna") for clean isolation. Telegram
bot, Groq key, and GitHub repo are still fully separate from vp-fa-scanner.
"""

import os
import sys
import time
import requests
from datetime import datetime, timezone
from supabase import create_client, ClientOptions

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://tradingqna.com"

# category_slug: (category_id, human label)
CATEGORIES = {
    "fundamental-analysis": (15, "Fundamental Analysis"),
    "technical-analysis": (16, "Technical Analysis"),
    "futures-options": (19, "F&O"),
    "stocks": (13, "Stocks"),
    "algos-strategies-code": (6, "Algos, strategies, code"),
    "nifty-banknifty": (37, "Nifty & Bank Nifty"),
    "the-daily-brief": (54, "The Daily Brief"),
}

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")  # optional — summarization skipped if absent

SCHEMA_NAME = "tqna"
TABLE_NAME = "topics"
HEADERS = {"User-Agent": "tqna-fa-scanner/1.0"}

# Point the client at the "tqna" schema so this scanner's data lives
# completely separately from vp-fa-scanner's tables in the same project.
sb = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
    options=ClientOptions(schema=SCHEMA_NAME),
)


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
    """Fetch the first post of a topic for an excerpt/summary source."""
    url = f"{BASE_URL}/t/{topic_id}.json"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    posts = resp.json().get("post_stream", {}).get("posts", [])
    if not posts:
        return ""
    raw = posts[0].get("cooked", "")
    # crude tag strip
    import re
    text = re.sub("<[^<]+?>", "", raw)
    return text.strip()[:1500]


# ---------------------------------------------------------------------------
# Groq summarization (optional)
# ---------------------------------------------------------------------------

def summarize_with_groq(title: str, body: str) -> str:
    """Summarize a topic's first post into 2-3 lines using Groq API."""
    if not GROQ_API_KEY or not body:
        return body[:300]
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": "openai/gpt-oss-20b",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Summarize this trading/investing forum post in 2-3 "
                            f"plain sentences.\n\nTitle: {title}\n\nPost:\n{body}"
                        ),
                    }
                ],
                "max_tokens": 200,
                "temperature": 0.3,
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"Groq summarization failed: {e}", file=sys.stderr)
        return body[:300]


# ---------------------------------------------------------------------------
# Supabase dedupe state
# ---------------------------------------------------------------------------

def get_known_topic(topic_id: int):
    res = sb.table(TABLE_NAME).select("*").eq("topic_id", topic_id).execute()
    return res.data[0] if res.data else None


def upsert_topic(topic_id: int, category_slug: str, last_posts_count: int, title: str):
    sb.table(TABLE_NAME).upsert({
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


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan_category(slug: str, category_id: int, label: str):
    topics = fetch_category_topics(category_id)
    for t in topics:
        topic_id = t["id"]
        title = t["title"]
        posts_count = t.get("posts_count", 1)
        topic_url = f"{BASE_URL}/t/{t['slug']}/{topic_id}"

        known = get_known_topic(topic_id)

        if known is None:
            # New topic
            excerpt = fetch_topic_excerpt(topic_id)
            summary = summarize_with_groq(title, excerpt)
            msg = (
                f"🆕 <b>{label}</b>\n"
                f"<b>{title}</b>\n\n"
                f"{summary}\n\n"
                f"<a href='{topic_url}'>Open topic</a>"
            )
            send_telegram(msg)
            upsert_topic(topic_id, slug, posts_count, title)

        elif posts_count > known["last_posts_count"]:
            # New reply(ies) on a known topic
            new_replies = posts_count - known["last_posts_count"]
            msg = (
                f"💬 <b>{label}</b> — {new_replies} new repl"
                f"{'y' if new_replies == 1 else 'ies'}\n"
                f"<b>{title}</b>\n"
                f"<a href='{topic_url}'>Open topic</a>"
            )
            send_telegram(msg)
            upsert_topic(topic_id, slug, posts_count, title)

        time.sleep(1)  # be polite to the forum API


def main():
    for slug, (category_id, label) in CATEGORIES.items():
        try:
            scan_category(slug, category_id, label)
        except Exception as e:
            print(f"Error scanning {slug}: {e}", file=sys.stderr)
        time.sleep(2)


if __name__ == "__main__":
    main()
