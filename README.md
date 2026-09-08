# TQNA FA/TA Scanner

Standalone scanner for tradingqna.com (Discourse forum, Zerodha's Q&A site).
Fully separate from the ValuePickr scanner — own Supabase project, own
Telegram bot, own Groq key, own GitHub repo.

## What it does

Every 3 hours, polls 7 categories (Fundamental Analysis, Technical Analysis,
F&O, Stocks, Algos/strategies/code, Nifty & Bank Nifty, The Daily Brief) via
the public Discourse JSON API. New topics get a full Telegram alert with a
Groq-generated 2-3 sentence summary. Existing topics with new replies get a
short activity ping. State is tracked in a Supabase table so nothing repeats.

## One-time setup

### 1. Supabase (new project — do not reuse the ValuePickr one)
1. Go to supabase.com → New project.
2. Once created, open the SQL editor and run the contents of `schema.sql`.
3. Go to Project Settings → API. Copy:
   - `Project URL` → this is `SUPABASE_URL`
   - `service_role` key (not the anon key) → this is `SUPABASE_KEY`

### 2. Telegram bot (new bot — do not reuse the ValuePickr one)
1. Message **@BotFather** on Telegram → `/newbot` → follow prompts.
2. Copy the token it gives you → this is `TELEGRAM_BOT_TOKEN`.
3. Create a channel or group for these alerts, add the bot to it as admin.
4. Get the chat ID:
   - Send any message in the channel/group.
   - Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser.
   - Find `"chat":{"id": ...}` in the response → this is `TELEGRAM_CHAT_ID`
     (for channels it's usually a negative number like `-1001234567890`).

### 3. Groq API key
1. Go to console.groq.com → API Keys → Create key.
2. Copy it → this is `GROQ_API_KEY`.

### 4. GitHub repo
1. Create a **new** repo, e.g. `tqna-scanner` (separate from vp-fa-scanner).
2. Push these files (`scanner.py`, `requirements.txt`, `schema.sql`,
   `.github/workflows/scan.yml`, this README) to it.
3. Go to repo Settings → Secrets and variables → Actions → New repository secret.
   Add each of these as a separate secret:
   - `SUPABASE_URL`
   - `SUPABASE_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `GROQ_API_KEY`

### 5. Test it
- Go to the repo's **Actions** tab → "TQNA FA/TA Scanner" workflow →
  **Run workflow** (manual trigger) to confirm it works before waiting for
  the first scheduled run.
- Check your Telegram channel for alerts, and check the `tqna_topics` table
  in Supabase to confirm rows are being inserted.

## Adjusting categories

Edit the `CATEGORIES` dict at the top of `scanner.py` — key is the URL slug,
value is `(category_id, display label)`. Get IDs from
`https://tradingqna.com/categories`.

## Cost notes
- Discourse JSON API: free, no auth needed.
- Groq: free tier available on llama-3.1-8b-instant; monitor usage on console.groq.com.
- Supabase: free tier is more than enough for this table size.
- GitHub Actions: free tier covers this easily (16 runs/day, ~1 min each).
