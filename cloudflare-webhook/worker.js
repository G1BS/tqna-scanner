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
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("ok", { status: 200 });
    }

    // Optional hardening: if you set a secret_token when calling Telegram's
    // setWebhook (see README), verify it here so only Telegram can hit this.
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
      return new Response("ok", { status: 200 }); // ignore malformed bodies
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

    // Ack immediately so you see a response within a second or two.
    await sendTelegram(env, "Scan triggered - starting shortly.");

    // Trigger the real scan workflow.
    const dispatchOk = await triggerScanWorkflow(env);
    if (!dispatchOk) {
      await sendTelegram(env, "Failed to trigger scan - check the GitHub token/permissions.");
    }

    return new Response("ok", { status: 200 });
  },
};

async function sendTelegram(env, text) {
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`;
  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
  });
}

async function triggerScanWorkflow(env) {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/scan.yml/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "tqna-scanner-webhook",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  return resp.status === 204;
}
