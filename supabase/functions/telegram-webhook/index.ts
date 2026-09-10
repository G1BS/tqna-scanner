// Supabase Edge Function: Telegram webhook receiver.
// Telegram calls this the instant a message is sent (no polling delay).
// On "/scan" from the configured chat, it immediately triggers the
// GitHub Actions scan workflow via workflow_dispatch and acks in Telegram.
//
// Deploy: supabase functions deploy telegram-webhook --no-verify-jwt
// Secrets needed (set via `supabase secrets set`):
//   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GITHUB_TOKEN, GITHUB_REPO (e.g. "G1BS/tqna-scanner")

Deno.serve(async (req) => {
  try {
    const body = await req.json();
    const msg = body.message ?? body.channel_post;
    if (!msg) {
      return new Response("ok", { status: 200 });
    }

    const chatId = String(msg.chat?.id ?? "");
    const text = String(msg.text ?? "").trim().toLowerCase();
    const expectedChatId = Deno.env.get("TELEGRAM_CHAT_ID") ?? "";

    if (chatId !== expectedChatId || !text.startsWith("/scan")) {
      return new Response("ignored", { status: 200 });
    }

    const botToken = Deno.env.get("TELEGRAM_BOT_TOKEN");
    const ghToken = Deno.env.get("GITHUB_TOKEN");
    const ghRepo = Deno.env.get("GITHUB_REPO");

    // Immediate ack so the user knows it was received.
    await fetch(`https://api.telegram.org/bot${botToken}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: expectedChatId,
        text: "🔎 Scan requested — running now, digest coming shortly...",
      }),
    });

    // Trigger the GitHub Actions workflow immediately.
    const dispatchResp = await fetch(
      `https://api.github.com/repos/${ghRepo}/actions/workflows/scan.yml/dispatches`,
      {
        method: "POST",
        headers: {
          "Authorization": `token ${ghToken}`,
          "Accept": "application/vnd.github+json",
        },
        body: JSON.stringify({ ref: "main" }),
      },
    );

    if (!dispatchResp.ok) {
      const errText = await dispatchResp.text();
      console.error("GitHub dispatch failed:", dispatchResp.status, errText);
      await fetch(`https://api.telegram.org/bot${botToken}/sendMessage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chat_id: expectedChatId,
          text: "⚠️ Couldn't start the scan — check the GitHub token/permissions.",
        }),
      });
    }

    return new Response("ok", { status: 200 });
  } catch (e) {
    console.error("Webhook error:", e);
    return new Response("error", { status: 500 });
  }
});
