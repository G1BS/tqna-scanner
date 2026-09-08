# TQNA FA/TA Scanner

Scanner for tradingqna.com (Discourse forum, Zerodha's Q&A site).
Shares the same Supabase project as vp-fa-scanner (free-tier caps you at 2
projects) but lives in its own Postgres schema (`tqna`) for clean isolation.
Own Telegram bot, own Groq key, own GitHub repo — only the DB host is shared.

## What it does

Every 3 hours:
1. Polls 4 categories (Fundamental Analysis, Technical Analysis, Algos/
   strategies/code, Nifty & Bank Nifty) — trimmed down from the original 7 to
   drop high-noise, low-signal categories (F&O, Stocks, Daily Brief).
2. **Stage 1 filter**: skips any topic whose title matches broker/support
   noise patterns (KYC, OTP, login issues, complaints, etc.) — free, no API
   calls spent on obvious junk.
3. **Stage 2 filter**: for new topics that survive Stage 1, one Groq call
   both scores (1-5, how valuable to an active trader/investor) and
   summarizes the post. Only topics scoring 4+ make it into the alert.
4. For existing topics, only alerts if they gained 3+ replies since the last
   scan (single-reply bumps are skipped).
5. Sends **one digest message per run** (not one message per topic), sorted
   by importance, with a color bar: 🟥 = score 5 (must-read), 🟧 = score 4.
   Active threads get a 💬 line. If nothing qualifies, no message is sent at
   all — no flooding.

State is tracked in `tqna.topics` (Supabase) so nothing repeats.

## Kill switch

To stop the scanner instantly — no code change, no GitHub secrets, no
waiting for a deploy — open the `tqna.settings` table in Supabase's Table
Editor and set `paused` to `true`. The next scheduled run will log a message
and exit immediately (no forum calls, no Groq spend, no Telegram messages).
Set it back to `false` whenever you want it to resume.

## One-time setup

### 1. Supabase (reuse the vp-fa-scanner project — new schema, not a new project)
1. Open the **existing** vp-fa-scanner Supabase project.
2. Open the SQL editor and run the contents of `schema.sql`. This creates
   the `tqna` schema with `topics` and `settings` tables, fully separate
   from vp-fa-scanner's own tables.
3. In a **separate** query, run `schema_grants.sql` (kept apart so a grants
   failure can't roll back the table creation above).
4. Go to Project Settings → API → **Exposed schemas**, and add `tqna` to the
   list (it only shows `public` by default). Without this step the scanner's
   API calls will fail. If you add it after tables already exist and still
   see "not found" errors, also run `NOTIFY pgrst, 'reload schema';` in the
   SQL editor to force an immediate cache refresh.
5. Copy the same `Project URL` and `service_role` key you already used for
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
   Note: uses `openai/gpt-oss-20b` — Groq deprecated the older Llama models.
   If Groq changes their lineup again, check console.groq.com/docs/deprecations
   and update the `model` field in `scanner.py`.

### 4. GitHub repo
1. Create a **new** repo, e.g. `tqna-scanner` (separate from vp-fa-scanner).
2. Push these files (`scanner.py`, `requirements.txt`, `schema.sql`,
   `schema_grants.sql`, `.github/workflows/scan.yml`, this README) to it.
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
- Check your Telegram channel for the digest, and check the `tqna.topics`
  table in Supabase to confirm rows are being inserted.
- The workflow also commits its own run output to `logs/last_run.log` in
  the repo — handy for debugging without needing to open the Actions log
  viewer.

## Adjusting things

- **Categories**: edit the `CATEGORIES` dict in `scanner.py`. Get IDs from
  `https://tradingqna.com/categories`.
- **Denylist keywords**: edit `TITLE_DENYLIST` in `scanner.py`.
- **Relevance bar**: edit `MIN_RELEVANCE_SCORE` (default 4 out of 5).
- **Reply-alert threshold**: edit `MIN_NEW_REPLIES_TO_ALERT` (default 3).

## Cost notes
- Discourse JSON API: free, no auth needed.
- Groq: free tier available; only spent on topics that pass Stage 1, and
  now one combined call does both scoring and summarizing (not two).
- Supabase: free tier is more than enough for this table size.
- GitHub Actions: free tier covers this easily (16 runs/day, ~1-2 min each).
