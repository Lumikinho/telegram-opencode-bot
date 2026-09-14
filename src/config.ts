import "dotenv/config";
import { homedir } from "node:os";

export const VERSION = "2.0.0-hybrid.1";

function intEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const n = parseInt(raw, 10);
  return Number.isFinite(n) ? n : fallback;
}

export const BOT_TOKEN = process.env.BOT_TOKEN ?? "";
export const OWNER_ID = intEnv("OWNER_ID", 0);
if (!OWNER_ID) {
  console.error("OWNER_ID não configurado — recusando iniciar por segurança.");
  process.exit(1);
}
export const CHAT_ID = process.env.CHAT_ID ?? "";
export const OPENCODE_DIR = process.env.OPENCODE_DIR ?? homedir();
export const OC_PORT = intEnv("OPENCODE_SERVER_PORT", 4100);
export const OC_URL =
  process.env.OPENCODE_SERVER_URL ?? `http://127.0.0.1:${OC_PORT}`;
export const OC_PASSWORD = process.env.OPENCODE_SERVER_PASSWORD ?? "";
export const BATTERY_PATH =
  process.env.BATTERY_PATH ?? "/sys/class/power_supply/battery";
export const BATTERY_CHECK_INTERVAL = intEnv("BATTERY_CHECK_INTERVAL", 30);
export const BATTERY_LOW_PCT = intEnv("BATTERY_LOW_PCT", 20);

export function ownerChatId(): number {
  if (CHAT_ID) {
    const n = parseInt(CHAT_ID, 10);
    if (Number.isFinite(n) && n !== 0) return n;
  }
  return OWNER_ID;
}
