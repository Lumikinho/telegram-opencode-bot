/** Validação de env com zod — falha rápido com mensagem clara. */
import "dotenv/config";
import { homedir } from "node:os";
import { z } from "zod";

const envSchema = z.object({
  BOT_TOKEN: z.string().min(1, "BOT_TOKEN é obrigatório (fale com o @BotFather)"),
  OWNER_ID: z.coerce.number().int().positive("OWNER_ID deve ser seu id numérico do Telegram"),
  CHAT_ID: z.string().default(""),
  OPENCODE_DIR: z.string().default(homedir()),
  OPENCODE_SERVER_PORT: z.coerce.number().int().positive().default(4100),
  OPENCODE_SERVER_URL: z.string().default(""),
  OPENCODE_SERVER_PASSWORD: z.string().default(""),
  WORKER_URL: z.string().default("http://127.0.0.1:8090"),
  APP_ENV: z.enum(["dev", "prod"]).default("dev"),
  PORT: z.coerce.number().int().positive().default(3000),
  WEBHOOK_DOMAIN: z.string().default(""),
  DATA_DIR: z.string().default("./data"),
  BATTERY_PATH: z.string().default("/sys/class/power_supply/battery"),
  BATTERY_CHECK_INTERVAL: z.coerce.number().int().positive().default(30),
  BATTERY_LOW_PCT: z.coerce.number().int().min(1).max(100).default(20),
  EXEC_ALLOWLIST: z.string().default(""),
  EXEC_TIMEOUT_MS: z.coerce.number().int().positive().default(30_000),
  EXEC_MAX_OUTPUT: z.coerce.number().int().positive().default(8000),
  EXEC_CWD: z.string().default(""),
});

const parsed = envSchema.safeParse(process.env);
if (!parsed.success) {
  console.error("Env inválido:");
  for (const issue of parsed.error.issues) {
    console.error(`  ${issue.path.join(".")}: ${issue.message}`);
  }
  process.exit(1);
}

const env = parsed.data;

export const VERSION = "0.1.0";
export const BOT_TOKEN = env.BOT_TOKEN;
export const OWNER_ID = env.OWNER_ID;
export const CHAT_ID = env.CHAT_ID;
export const OPENCODE_DIR = env.OPENCODE_DIR;
export const OC_PORT = env.OPENCODE_SERVER_PORT;
export const OC_URL = env.OPENCODE_SERVER_URL || `http://127.0.0.1:${OC_PORT}`;
export const OC_PASSWORD = env.OPENCODE_SERVER_PASSWORD;
export const WORKER_URL = env.WORKER_URL.replace(/\/$/, "");
export const APP_ENV = env.APP_ENV;
export const PORT = env.PORT;
export const WEBHOOK_DOMAIN = env.WEBHOOK_DOMAIN;
export const DATA_DIR = env.DATA_DIR;
export const BATTERY_PATH = env.BATTERY_PATH;
export const BATTERY_CHECK_INTERVAL = env.BATTERY_CHECK_INTERVAL;
export const BATTERY_LOW_PCT = env.BATTERY_LOW_PCT;

export function ownerChatId(): number {
  if (CHAT_ID) {
    const n = parseInt(CHAT_ID, 10);
    if (Number.isFinite(n) && n !== 0) return n;
  }
  return OWNER_ID;
}
