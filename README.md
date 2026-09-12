# TQNA FA/TA Scanner

Scanner for tradingqna.com (Discourse forum, Zerodha's Q&A site).
Shares the same Supabase project as vp-fa-scanner (free-tier caps you at 2
projects) but lives in its own Postgres schema (`tqna`) for clean isolation.
Own Telegram bot, own Groq key, own GitHub repo — only the DB host is shared.

## What it does

Every 3 hours:
1. Polls all 7 categories (Fundamental Analysis, Technical Analysis, F&O,
   Stocks, Algos/strategies/code, Nifty & Bank Nifty, The Daily Brief) —
   nothing dropped by category, since hypothesis-worthy signal shows up
   across all of them. Noise is filtered by content, not by category.
2. **Stage 1 filter**: skips any topic whose title matches broker/support
   noise patterns (KYC, OTP, login issues, complaints, etc.) — free, no API
   calls spent on obvious junk.
3. **Stage 2 filter**: for new topics that survive Stage 1, one Groq call
   both scores (1-5, how valuable to an active trader/investor) and
   summarizes the post. Only topics scoring 4+ make it into the alert.
4. For existing topics, only alerts if they gained 3+ replies since the last
   scan (single-reply bumps are skipped).
5. **Daily alert cap (20/day by default)**: even after filtering, a busy day
   or a big backlog (e.g. the very first run) could still send a lot. A
   rolling daily counter caps total alerts (new topics + active threads
   combined) at `DAILY_ALERT_CAP`, always keeping the highest-scoring items
   first — anything beyond the cap on a given day is simply not alerted
   (though it's still recorded, so it won't be re-scored pointlessly later).
6. Sends **one digest message per run** (not one message per topic), sorted
   by importance, with a color bar: 🟥 = score 5 (must-read), 🟧 = score 4.
   Active threads get a 💬 line. If nothing qualifies, no message is sent at
   all — no flooding.
7. Groq calls are paced (3s apart) and back off on rate limits (respecting
   the `Retry-After` header when Groq sends one), with a resilient JSON
   parser that salvages a score even if the model's response gets cut off.

State is tracked in `tqna.topics` (Supabase) so nothing repeats.

## Kill switch

To stop the scanner instantly — no code change, no GitHub secrets, no
waiting for a deploy — open the `tqna.settings` table in Supabase's Table
Editor and set `paused` to `true`. The next scheduled run will log a message
and exit immediately (no forum calls, no Groq spend, no Telegram messages).
Set it back to `false` whenever you want it to resume.

## On-demand scans

Two ways to trigger a scan outside the daily 4 PM UTC schedule:

1. **Telegram command (instant, ~1-2 sec)**: send `scan` (or `/scan`, `run`,
   `/run`) in the bot's chat/channel. A Cloudflare Worker receives it the
   moment you send it — Telegram pushes the message to the Worker directly,
   there's no polling/cron involved — and immediately triggers the GitHub
   scan. One-time setup below (~10 minutes, browser only, no CLI).
2. **GitHub Actions manual trigger**: Actions tab → "TQNA FA/TA Scanner" →
   Run workflow (works from the GitHub mobile app too).

### Why not GitHub Actions cron for this?

We tried that first (checking Telegram every 5 minutes via a scheduled
workflow). It's unreliable: GitHub deliberately deprioritizes and delays
frequent scheduled workflows, so "every 5 minutes" can actually mean
2-5 hour gaps in practice. A webhook (Telegram pushes to us) doesn't have
this problem — there's no schedule to be late for.

### Setting up the instant webhook (one-time, ~10 min, browser only)

**Step 1 — Create a Cloudflare account (free)**
Go to https://dash.cloudflare.com/sign-up and sign up (email + password).
No credit card needed for the free tier.

**Step 2 — Create the Worker**
1. In the Cloudflare dashboard, go to **Workers & Pages** (left sidebar).
2. Click **Create** → **Workers** → **Create Worker**.
3. Give it a name, e.g. `tqna-telegram-webhook` — this becomes part of its
   URL (`https://tqna-telegram-webhook.<your-subdomain>.workers.dev`).
4. Click **Deploy** to create it with the default "Hello World" code (we'll
   replace this next).

**Step 3 — Paste the actual code**
1. On the Worker's page, click **Edit code** (sometimes labeled "Quick edit").
2. Delete everything in the editor and paste the full contents of
   `cloudflare-webhook/worker.js` from this repo.
3. Click **Save and deploy**.

**Step 4 — Add the secrets it needs**
1. Go to the Worker's **Settings** tab → **Variables and Secrets**.
2. Add each of these as a variable, clicking **Encrypt** for each so they're
   stored as secrets (not visible in plain text afterward):
   - `TELEGRAM_BOT_TOKEN` — your tqna bot's token (same one already in the
     GitHub repo secrets)
   - `TELEGRAM_CHAT_ID` — your tqna chat ID (same one already in GitHub)
   - `GITHUB_REPO` — `G1BS/tqna-scanner`
   - `GITHUB_TOKEN` — a **new** GitHub PAT (see Step 5 — don't reuse an
     existing one, keep this scoped narrowly)
3. Click **Save and deploy** again after adding them.

**Step 5 — Create the GitHub token the Worker will use**
1. Go to https://github.com/settings/tokens?type=beta (fine-grained tokens
   — narrower and safer than a classic PAT).
2. Click **Generate new token**.
3. Under **Repository access**, choose **Only select repositories** →
   pick `tqna-scanner`.
4. Under **Permissions** → **Repository permissions**, find **Actions** and
   set it to **Read and write**.
5. Generate the token, copy it, and paste it as the Worker's `GITHUB_TOKEN`
   secret from Step 4 (you won't be able to see it again after leaving the
   page, so paste it right away).

**Step 6 — Point Telegram at the Worker**
Get your Worker's URL from its Cloudflare dashboard page (looks like
`https://tqna-telegram-webhook.<subdomain>.workers.dev`). Then, from any
terminal (your phone's browser address bar works too, since this is just a
GET request — or use a site like reqbin.com to fire it):
```
https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=<YOUR_WORKER_URL>
```
Replace `<YOUR_BOT_TOKEN>` with your tqna bot's token and `<YOUR_WORKER_URL>`
with the Worker URL. You should get back `{"ok":true,"result":true,...}`.

**Step 7 — Test it**
Send `scan` in the Telegram chat. You should see "Scan triggered - starting
shortly." within 1-2 seconds, followed by the real digest once the scan
workflow completes.

### Optional hardening
To stop random internet traffic from hitting your Worker URL and pretending
to be Telegram, set a secret token when calling `setWebhook`:
```
https://api.telegram.org/bot<TOKEN>/setWebhook?url=<WORKER_URL>&secret_token=<make up a random string>
```
Then add that same random string as the Worker's `WEBHOOK_SECRET` variable
(Step 4) — the code already checks for it if present.

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
- **Daily alert cap**: edit `DAILY_ALERT_CAP` in `scanner.py` (default 20/day).

## Cost notes
- Discourse JSON API: free, no auth needed.
- Groq: free tier available; only spent on topics that pass Stage 1, and
  now one combined call does both scoring and summarizing (not two).
- Supabase: free tier is more than enough for this table size.
- GitHub Actions: free tier covers this easily (16 runs/day, ~1-2 min each).
