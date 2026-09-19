/** Entrypoint: polling em dev, webhook em prod (Bun.serve). */
import { webhookCallback } from "grammy";
import { APP_ENV, BOT_TOKEN, OC_URL, OPENCODE_DIR, PORT, VERSION, WEBHOOK_DOMAIN } from "./config/env.ts";
import { bootstrap, stopHook } from "./bot.ts";

if (!BOT_TOKEN) {
  console.error("BOT_TOKEN não configurado. Copie .env.example para .env e preencha.");
  process.exit(1);
}

console.log("=".repeat(44));
console.log(`  meu-bot  v${VERSION}  [${APP_ENV}]`);
console.log(`  opencode=${OC_URL}  dir=${OPENCODE_DIR}`);
console.log("=".repeat(44));

const bot = await bootstrap();

function onStop(): void {
  void stopHook().finally(() => process.exit(0));
}
process.on("SIGINT", onStop);
process.on("SIGTERM", onStop);

if (APP_ENV === "prod" && WEBHOOK_DOMAIN) {
  const path = `/webhook/${BOT_TOKEN.split(":")[0]}`;
  const handle = webhookCallback(bot, "bun");
  Bun.serve({
    port: PORT,
    fetch(req) {
      const url = new URL(req.url);
      if (url.pathname === "/healthz") return new Response('{"ok":true}');
      if (url.pathname === path && req.method === "POST") return handle(req);
      return new Response("meu-bot", { status: 404 });
    },
  });
  await bot.api.setWebhook(`https://${WEBHOOK_DOMAIN}${path}`, { drop_pending_updates: true });
  console.log(`webhook em https://${WEBHOOK_DOMAIN}${path} (porta ${PORT})`);
} else {
  await bot.api.deleteWebhook({ drop_pending_updates: true }).catch(() => {});
  await bot.start({ drop_pending_updates: true });
}
