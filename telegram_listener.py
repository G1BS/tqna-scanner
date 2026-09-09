"""
Polls Telegram for a /scan command in the configured chat and, if found,
runs the scanner immediately. Meant to be run every few minutes via GitHub
Actions (see .github/workflows/telegram_listener.yml) — no persistent
server needed.

Tracks the last processed Telegram update ID in tqna.settings so the same
command is never processed twice, even across separate workflow runs.
"""

import os
import sys
import requests
from supabase import create_client, ClientOptions

import scanner  # reuse the same scan logic, Supabase client, and env config

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])

SCHEMA_NAME = "tqna"
SETTINGS_TABLE = "settings"

sb = create_client(SUPABASE_URL, SUPABASE_KEY, options=ClientOptions(schema=SCHEMA_NAME))


def get_last_update_id() -> int:
    try:
        res = sb.table(SETTINGS_TABLE).select("last_telegram_update_id").eq("id", 1).execute()
        if res.data:
            return int(res.data[0].get("last_telegram_update_id") or 0)
    except Exception as e:
        print(f"Failed to read last_telegram_update_id (defaulting to 0): {e}", file=sys.stderr)
    return 0


def set_last_update_id(update_id: int):
    try:
        sb.table(SETTINGS_TABLE).update({"last_telegram_update_id": update_id}).eq("id", 1).execute()
    except Exception as e:
        print(f"Failed to save last_telegram_update_id: {e}", file=sys.stderr)


def send_reply(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)


def main():
    last_id = get_last_update_id()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    resp = requests.get(url, params={"offset": last_id + 1, "timeout": 0}, timeout=20)
    resp.raise_for_status()
    updates = resp.json().get("result", [])

    if not updates:
        print("No new Telegram updates.")
        return

    highest_id = last_id
    scan_requested = False

    for u in updates:
        highest_id = max(highest_id, u["update_id"])
        msg = u.get("message") or u.get("channel_post")
        if not msg:
            continue

        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip().lower()

        if chat_id == TELEGRAM_CHAT_ID and text in ("/scan", "/scan@" ):
            scan_requested = True
        elif chat_id == TELEGRAM_CHAT_ID and text.startswith("/scan"):
            scan_requested = True  # covers "/scan@YourBotName" form

    # Always advance the offset so processed (or irrelevant) updates aren't
    # re-fetched next time, regardless of whether /scan was found.
    set_last_update_id(highest_id)

    if scan_requested:
        print("/scan command received — running scan now.")
        send_reply("🔎 Scan requested — running now, digest coming shortly...")
        scanner.main()
        send_reply("✅ Scan complete.")
    else:
        print("No /scan command in new updates.")


if __name__ == "__main__":
    main()
