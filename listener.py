"""
Lightweight listener: checks Telegram for a "/scan" command, and if found,
triggers the real scan workflow via the GitHub API using the auto-provided
GITHUB_TOKEN (no manual PAT needed). Runs every 5 minutes, costs zero LLM
tokens (only calls Telegram's getUpdates and, when needed, the GitHub API).

Mirrors the same working pattern used in vp-fa-pulse's listener.py.
"""

import os
import sys
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")  # auto-set by Actions

TRIGGER_WORDS = {"/scan", "scan", "/run", "run"}
SCHEMA_NAME = "tqna"
STATE_TABLE = "settings"
STATE_ROW_ID = 1


def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept-Profile": SCHEMA_NAME,
        "Content-Profile": SCHEMA_NAME,
    }


def get_last_update_id() -> int:
    url = f"{SUPABASE_URL}/rest/v1/{STATE_TABLE}"
    params = {"id": f"eq.{STATE_ROW_ID}", "select": "last_telegram_update_id"}
    resp = requests.get(url, headers=supabase_headers(), params=params, timeout=15)
    resp.raise_for_status()
    rows = resp.json()
    return int(rows[0]["last_telegram_update_id"]) if rows else 0


def set_last_update_id(update_id: int):
    url = f"{SUPABASE_URL}/rest/v1/{STATE_TABLE}"
    params = {"id": f"eq.{STATE_ROW_ID}"}
    resp = requests.patch(url, headers=supabase_headers(), params=params,
                           json={"last_telegram_update_id": update_id}, timeout=15)
    resp.raise_for_status()


def get_telegram_updates(offset: int):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    resp = requests.get(url, params={"offset": offset, "timeout": 0}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("result", [])


def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)


def trigger_scan_workflow():
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/workflows/scan.yml/dispatches"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    resp = requests.post(url, headers=headers, json={"ref": "main"}, timeout=15)
    return resp.status_code == 204


def main():
    missing = [n for n in ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SUPABASE_URL", "SUPABASE_KEY",
                            "GITHUB_TOKEN", "GITHUB_REPOSITORY"] if not os.environ.get(n)]
    if missing:
        print(f"Missing env vars: {missing}", file=sys.stderr)
        sys.exit(1)

    last_update_id = get_last_update_id()
    updates = get_telegram_updates(offset=last_update_id + 1)

    if not updates:
        print("No new Telegram messages.")
        return

    max_seen_id = last_update_id
    triggered = False

    for update in updates:
        update_id = update.get("update_id", 0)
        max_seen_id = max(max_seen_id, update_id)

        message = update.get("message") or update.get("channel_post") or {}
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip().lower()

        if chat_id != str(TELEGRAM_CHAT_ID):
            print(f"Ignoring message from unrecognized chat_id {chat_id}")
            continue

        if text in TRIGGER_WORDS and not triggered:
            print(f"Trigger word '{text}' received, dispatching scan workflow.")
            ok = trigger_scan_workflow()
            if ok:
                send_telegram("Scan triggered - starting shortly.")
                triggered = True
            else:
                send_telegram("Failed to trigger scan - check the listener workflow logs.")

    set_last_update_id(max_seen_id)
    print("Done.")


if __name__ == "__main__":
    main()
