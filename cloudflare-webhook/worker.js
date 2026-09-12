// Cloudflare Worker — Telegram webhook receiver for tqna-scanner.
// Telegram calls this the INSTANT a message is sent (true push, not polling),
// so response time is ~1-2 seconds instead of GitHub's unreliable cron delay.
//
// On "scan" / "/scan" / "run" / "/run" from the configured chat, it:
//   1. Replies immediately in Telegram ("Scan triggered - starting shortly.")
//   2. Triggers the scan.yml workflow via GitHub's workflow_dispatch API
//
// Deploy: paste this file's contents into the Cloudflare dashboard's
// Worker code editor (see README section "Setting up the instant webhook").
// No CLI, no npm install, no login flow beyond a normal Cloudflare signup.

const TRIGGER_WORDS = new Set(["scan", "/scan", "run", "/run"]);

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("ok", { status: 200 });
    }

    if (env.WEBHOOK_SECRET) {
      const got = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
      if (got !== env.WEBHOOK_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return new Response("ok", { status: 200 });
    }

    const msg = body.message || body.channel_post;
    if (!msg) {
      return new Response("ok", { status: 200 });
    }

    const chatId = String(msg.chat?.id ?? "");
    const text = String(msg.text ?? "").trim().toLowerCase();

    if (chatId !== String(env.TELEGRAM_CHAT_ID) || !TRIGGER_WORDS.has(text)) {
      return new Response("ignored", { status: 200 });
    }

    // Everything below is wrapped so ANY failure (bad token, malformed env
    // var, network hiccup) always gets reported back to Telegram instead
    // of silently dying with no explanation.
    ctx.waitUntil(handleScanRequest(env));

    return new Response("ok", { status: 200 });
  },
};

async function handleScanRequest(env) {
  try {
    await sendTelegram(env, "Scan triggered - starting shortly.");
  } catch (e) {
    // If even the ack fails, we truly can't report anything — just log it
    // for Cloudflare's own error tracking as a last resort.
    console.error("Failed to send ack message:", e);
    return;
  }

  try {
    const result = await triggerScanWorkflow(env);
    if (!result.ok) {
      await sendTelegram(
        env,
        `⚠️ Failed to trigger scan.\nStatus: ${result.status}\nDetails: ${result.detail}`.slice(0, 3900)
      );
    }
  } catch (e) {
    await sendTelegram(env, `⚠️ Failed to trigger scan (exception): ${String(e).slice(0, 3800)}`);
  }
}

async function sendTelegram(env, text) {
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`;
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
  });
  if (!resp.ok) {
    const errText = await resp.text();
    console.error("sendTelegram failed:", resp.status, errText);
  }
}

async function triggerScanWorkflow(env) {
  const repo = (env.GITHUB_REPO || "").trim();
  const token = (env.GITHUB_TOKEN || "").trim();
  const url = `https://api.github.com/repos/${repo}/actions/workflows/scan.yml/dispatches`;

  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "tqna-scanner-webhook",
    },
    body: JSON.stringify({ ref: "main" }),
  });

  if (resp.status === 204) {
    return { ok: true, status: 204, detail: "" };
  }

  const detail = await resp.text();
  return { ok: false, status: resp.status, detail };
}
