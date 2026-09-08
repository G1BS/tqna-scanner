# TQNA FA/TA Scanner

Scanner for tradingqna.com (Discourse forum, Zerodha's Q&A site).
Shares the same Supabase project as vp-fa-scanner (free-tier caps you at 2
projects) but lives in its own Postgres schema (`tqna`) for clean isolation.
Own Telegram bot, own Groq key, own GitHub repo — only the DB host is shared.

## What it does

Every 3 hours, polls 7 categories (Fundamental Analysis, Technical Analysis,
F&O, Stocks, Algos/strategies/code, Nifty & Bank Nifty, The Daily Brief) via
the public Discourse JSON API. New topics get a full Telegram alert with a
Groq-generated 2-3 sentence summary. Existing topics with new replies get a
short activity ping. State is tracked in a Supabase table so nothing repeats.

## One-time setup

### 1. Supabase (reuse the vp-fa-scanner project — new schema, not a new project)
1. Open the **existing** vp-fa-scanner Supabase project.
2. Open the SQL editor and run the contents of `schema.sql`. This creates a
   new `tqna` schema with its own `topics` table, fully separate from
   vp-fa-scanner's `public.vp_fa_topics` table.
3. Go to Project Settings → API → **Exposed schemas**, and add `tqna` to the
   list (it only shows `public` by default). Without this step the scanner's
   API calls will fail.
4. Copy the same `Project URL` and `service_role` key you already used for
   vp-fa-scanner → these are `SUPABASE_URL` / `SUPABASE_KEY` for this project too.

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
