// Cloudflare Worker — Telegram webhook → GitHub repository_dispatch
//
// Receives Telegram updates for @artur_capture_bot. Filters messages in the
// Knowledge Capture channel/group. Dispatches a `user-reply` event to the
// `artsteff/knowledge_mngmnt` repo with the raw text. The intent router inside
// GH Actions decides what to do.
//
// Env vars (Cloudflare dashboard → Settings → Variables):
//   GH_DISPATCH_TOKEN          PAT with `repo` scope on artsteff/knowledge_mngmnt
//   WEBHOOK_SHARED_SECRET      same value set in Telegram setWebhook secret_token
//   TELEGRAM_CAPTURE_CHANNEL_ID  e.g. -1001234567890
//
// Deploy with `wrangler deploy`. Then:
//   curl -X POST "https://api.telegram.org/bot$BOT_TOKEN/setWebhook" \
//        -d url=https://capture.<acct>.workers.dev/tg-webhook \
//        -d secret_token=$WEBHOOK_SHARED_SECRET

export interface Env {
  GH_DISPATCH_TOKEN: string;
  WEBHOOK_SHARED_SECRET: string;
  TELEGRAM_CAPTURE_CHANNEL_ID: string;
}

const GH_OWNER = "artsteff";
const GH_REPO = "knowledge_mngmnt";

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    if (req.method !== "POST") return new Response("method", { status: 405 });

    const url = new URL(req.url);
    if (url.pathname !== "/tg-webhook") return new Response("not found", { status: 404 });

    const header = req.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (header !== env.WEBHOOK_SHARED_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update: any;
    try {
      update = await req.json();
    } catch {
      return new Response("bad json", { status: 400 });
    }

    const msg = update.message ?? update.edited_message ?? update.channel_post;
    if (!msg) return new Response("ignored", { status: 200 });

    const chatId = String(msg.chat?.id ?? "");
    if (chatId !== String(env.TELEGRAM_CAPTURE_CHANNEL_ID)) {
      return new Response("wrong chat", { status: 200 });
    }

    const text = (msg.text ?? "").trim();
    if (!text) return new Response("no text", { status: 200 });

    const payload = {
      reply_text: text,
      chat_id: chatId,
      message_id: msg.message_id,
      reply_to_message_id: msg.reply_to_message?.message_id ?? null,
      ts: msg.date,
    };

    const gh = await fetch(
      `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "knowledge-mngmnt-worker",
        },
        body: JSON.stringify({
          event_type: "user-reply",
          client_payload: payload,
        }),
      },
    );

    if (!gh.ok) {
      const body = await gh.text();
      return new Response(`gh ${gh.status}: ${body}`, { status: 502 });
    }
    return new Response("dispatched", { status: 200 });
  },
};
