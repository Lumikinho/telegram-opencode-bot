/** Instância do bot grammY + registro de middlewares, comandos e handlers. */
import { Bot } from "grammy";
import { BOT_TOKEN, ownerChatId } from "./config/env.ts";
import { ownerOnly } from "./middlewares/auth.ts";
import { logging } from "./middlewares/logging.ts";
import { rateLimit } from "./middlewares/ratelimit.ts";
import { COMMANDS, registerCommands } from "./commands/index.ts";
import { registerCallbacks } from "./handlers/callbacks.ts";
import { registerMessages } from "./handlers/messages.ts";
import { batteryWatch } from "./services/battery.ts";
import {
  bootSidSet,
  cfgFor,
  getSessions,
  persistChat,
  refreshSessionsAfterCycle,
  restoreChats,
  setSessions,
} from "./services/store.ts";
import {
  consumeEvents,
  createSession,
  ensureServer,
  listSessions,
  restoreChatSession,
  stopServer,
  type ServerEvent,
} from "./services/opencode.ts";
import { routeEvent } from "./services/turns.ts";
import { getStartupInfo, startupMarkdown } from "./utils/version.ts";
import { turnTelegramHtml } from "./services/python.ts";
import { shouldKeepServerOnExit, type RestartHooks } from "./services/restart.ts";

let stopSse = false;

const restartHooks: RestartHooks = { didCycleServer: refreshSessionsAfterCycle };

async function ensureSidFor(ownerId: number): Promise<void> {
  const cfg = cfgFor(ownerId);
  if (cfg.sid) return;
  const { sid } = await restoreChatSession(cfg, getSessions(), bootSidSet());
  if (sid) {
    bootSidSet().add(sid);
    persistChat(ownerId);
  }
}

export function createBot(): Bot {
  const bot = new Bot(BOT_TOKEN);
  logging(bot);
  bot.use(ownerOnly);
  bot.use(rateLimit);
  registerCommands(bot, restartHooks);
  registerCallbacks(bot, restartHooks);
  registerMessages(bot);
  return bot;
}

export async function bootstrap(): Promise<Bot> {
  restoreChats();
  await ensureServer();
  setSessions(await listSessions());
  const bot = createBot();
  void consumeEvents((ev: ServerEvent) => void routeEvent(bot, ev), () => stopSse);
  void batteryWatch(bot, () => stopSse).catch((e) => console.warn("batteryWatch saiu:", e));
  await announceOnline(bot).catch((e) => console.warn("aviso de boot falhou:", e));
  await ensureSidFor(ownerChatId());
  return bot;
}

async function announceOnline(bot: Bot): Promise<void> {
  const info = await getStartupInfo();
  const html = await turnTelegramHtml(startupMarkdown(info), 3500);
  await bot.api.sendMessage(ownerChatId(), html, { parse_mode: "HTML" });
}

export function shutdown(): void {
  stopSse = true;
}

export async function stopHook(): Promise<void> {
  shutdown();
  if (shouldKeepServerOnExit()) return;
  await stopServer();
}

export { COMMANDS, createSession };
