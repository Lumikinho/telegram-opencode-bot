import { BOT_TOKEN, OPENCODE_DIR, OC_URL, VERSION } from "./config.ts";
import { bootstrap, shutdown } from "./telegram.ts";
import { stopServer } from "./opencode.ts";

if (!BOT_TOKEN) {
  console.error("BOT_TOKEN não configurado. Crie um .env com BOT_TOKEN=seu_token");
  process.exit(1);
}

console.log("=".repeat(44));
console.log(`  Telegram opencode bot híbrido  v${VERSION}`);
console.log(`  url=${OC_URL}  dir=${OPENCODE_DIR}`);
console.log("=".repeat(44));

const bot = await bootstrap();

function onStop(): void {
  shutdown();
  void stopServer().finally(() => process.exit(0));
}
process.on("SIGINT", onStop);
process.on("SIGTERM", onStop);

await bot.start({ drop_pending_updates: true });
