/** Bot Telegram (grammy): auth do dono, comandos e texto livre -> opencode v2. */
import { Bot, InlineKeyboard } from "grammy";
import {
  BATTERY_LOW_PCT,
  BOT_TOKEN,
  OC_PORT,
  OC_URL,
  OPENCODE_DIR,
  OWNER_ID,
  VERSION,
  ownerChatId,
} from "./config.ts";
import {
  answerForm,
  answerPermission,
  consumeEvents,
  createSession,
  ensureServer,
  interruptSession,
  listAgents,
  listModels,
  listSessions,
  rejectForm,
  restoreChatSession,
  sendMessage,
  serverInfo,
  stopServer,
  type ServerEvent,
  type SessionSummary,
} from "./opencode.ts";
import { batteryOnce, funnelOff, funnelOn, funnelStatus } from "./workers.ts";
import { loadChats, saveChat } from "./store.ts";
import {
  askCustom,
  cancelTurn,
  chooseOption,
  consumeCustomAnswer,
  getTurn,
  rejectQuestions,
  replyPermission,
  routeEvent,
  startTurn,
  submitQuestions,
  toggleOption,
} from "./turns.ts";

interface ChatCfg {
  sid?: string | null;
  model?: string;
  agent?: string;
}

const chats = new Map<number, ChatCfg>();
let sessions: SessionSummary[] = [];
let stopSse = false;
let storeReady = false;

function cfgFor(chatId: number): ChatCfg {
  let c = chats.get(chatId);
  if (!c) {
    c = {};
    chats.set(chatId, c);
  }
  return c;
}

function persist(chatId: number): void {
  try {
    saveChat(chatId, cfgFor(chatId));
  } catch (e) {
    console.warn("store: falha ao salvar chat", chatId, e);
  }
}

function isOwner(id?: number): boolean {
  return id === OWNER_ID;
}

async function ensureSid(chatId: number): Promise<string | null> {
  const cfg = cfgFor(chatId);
  if (cfg.sid) return cfg.sid;
  const { sid } = await restoreChatSession(cfg, sessions);
  if (sid) persist(chatId);
  return sid;
}

export function menuKeyboard(): InlineKeyboard {
  return new InlineKeyboard()
    .text("🆕 nova", "menu:new")
    .text("📊 status", "menu:status")
    .row()
    .text("🤖 modelos", "menu:models")
    .text("🧑‍💻 agentes", "menu:agents")
    .row()
    .text("🔌 funnel on", "menu:funnel-on")
    .text("🔌 funnel off", "menu:funnel-off");
}

export function createBot(): Bot {
  const bot = new Bot(BOT_TOKEN);

  bot.use(async (ctx, next) => {
    if (!isOwner(ctx.from?.id)) {
      await ctx.reply("⛔ Acesso negado.");
      return;
    }
    await next();
  });

  bot.command("start", (ctx) => ctx.reply(
    "opencode bot híbrido (Bun+TS+Python) online.\nUse /menu, /new, /status, /bateria, /funnel.",
  ));
  bot.command("help", (ctx) => ctx.reply(
    "/menu /new /cancel /status /bateria /funnel /models /agents /sessions /restart",
  ));
  bot.command("menu", (ctx) => ctx.reply("Painel:", { reply_markup: menuKeyboard() }));

  bot.command("new", async (ctx) => {
    const sid = await createSession();
    cfgFor(ctx.chat.id).sid = sid;
    persist(ctx.chat.id);
    sessions.push({ id: sid, time: { updated: 0 } });
    await ctx.reply(`🆕 sessão criada: \`${sid}\``, { parse_mode: "Markdown" });
  });

  bot.command("cancel", async (ctx) => {
    const sid = cfgFor(ctx.chat.id).sid;
    if (sid) await interruptSession(sid);
    const had = await cancelTurn(bot, ctx.chat.id);
    await ctx.reply(had ? "⏹ Resposta interrompida." : "⏹ Nada em andamento.");
  });

  bot.command("status", async (ctx) => {
    const info = await serverInfo();
    const dot = info.ok ? "🟢" : "🔴";
    await ctx.reply(
      `${dot} servidor ${info.status} (${info.url}:${info.port})\n` +
        `dir: \`${OPENCODE_DIR}\`\nversão bot: \`${VERSION}\``,
      { parse_mode: "Markdown" },
    );
  });

  bot.command("bateria", async (ctx) => {
    try {
      const b = await batteryOnce();
      await ctx.reply(b.formatted, { parse_mode: "Markdown" });
    } catch (e) {
      await ctx.reply(`❌ bateria indisponível: ${e}`.slice(0, 400));
    }
  });

  bot.command("funnel", async (ctx) => {
    const arg = ctx.match?.toString().trim().toLowerCase();
    try {
      if (arg === "on") {
        const r = await funnelOn();
        await ctx.reply(r.message, { parse_mode: "Markdown" });
      } else if (arg === "off") {
        const r = await funnelOff();
        await ctx.reply(r.message, { parse_mode: "Markdown" });
      } else {
        const s = await funnelStatus();
        await ctx.reply(s.raw ? `\`\`\`\n${s.raw.slice(0, 3500)}\n\`\`\`` : "(sem funnel ativo)", {
          parse_mode: "Markdown",
        });
      }
    } catch (e) {
      await ctx.reply(`❌ funnel: ${e}`.slice(0, 400));
    }
  });

  bot.command("models", async (ctx) => {
    const arg = ctx.match?.toString().trim();
    if (arg) {
      cfgFor(ctx.chat.id).model = arg;
      persist(ctx.chat.id);
      await ctx.reply(`Modelo: \`${arg}\``, { parse_mode: "Markdown" });
      return;
    }
    const models = await listModels();
    await ctx.reply(models.length ? models.slice(0, 50).join("\n") : "(sem modelos)");
  });

  bot.command("agents", async (ctx) => {
    const arg = ctx.match?.toString().trim();
    if (arg) {
      cfgFor(ctx.chat.id).agent = arg;
      persist(ctx.chat.id);
      await ctx.reply(`Agente: \`${arg}\``, { parse_mode: "Markdown" });
      return;
    }
    const agents = await listAgents();
    await ctx.reply(agents.length ? agents.join("\n") : "(sem agentes)");
  });

  bot.command("sessions", async (ctx) => {
    sessions = await listSessions();
    await ctx.reply(sessions.length ? sessions.map((s) => s.id).slice(0, 20).join("\n") : "(sem sessões)");
  });

  bot.command("restart", async (ctx) => {
    const arg = (ctx.match?.toString().trim().toLowerCase() || "bot") as string;
    if (arg === "servidor" || arg === "server") {
      await stopServer();
      await ensureServer();
      await ctx.reply("🔄 servidor reiniciado.");
    } else {
      await ctx.reply("🔄 reinicie o processo (systemd/tmux).");
    }
  });

  bot.callbackQuery("menu:new", async (ctx) => {
    const sid = await createSession();
    cfgFor(ctx.chat!.id).sid = sid;
    persist(ctx.chat!.id);
    await ctx.answerCallbackQuery("sessão criada");
    await ctx.reply(`🆕 \`${sid}\``, { parse_mode: "Markdown" });
  });
  bot.callbackQuery("menu:status", async (ctx) => {
    const info = await serverInfo();
    await ctx.answerCallbackQuery(info.ok ? "servidor ativo" : "servidor parado");
    await ctx.reply(`${info.ok ? "🟢" : "🔴"} ${info.status} — ${info.url}`);
  });
  bot.callbackQuery("menu:models", async (ctx) => {
    const models = await listModels();
    await ctx.answerCallbackQuery();
    await ctx.reply(models.slice(0, 30).join("\n") || "(sem modelos)");
  });
  bot.callbackQuery("menu:agents", async (ctx) => {
    const agents = await listAgents();
    await ctx.answerCallbackQuery();
    await ctx.reply(agents.join("\n") || "(sem agentes)");
  });
  bot.callbackQuery("menu:funnel-on", async (ctx) => {
    const r = await funnelOn();
    await ctx.answerCallbackQuery(r.ok ? "funnel on" : "falhou");
    await ctx.reply(r.message, { parse_mode: "Markdown" });
  });
  bot.callbackQuery("menu:funnel-off", async (ctx) => {
    const r = await funnelOff();
    await ctx.answerCallbackQuery(r.ok ? "funnel off" : "falhou");
    await ctx.reply(r.message, { parse_mode: "Markdown" });
  });

  // Permissões e forms v2 via botões (perm:/qo:/qt:/qs:/qr:/qc:) ou texto
  // ("allow:<id>", "form:<id> {...}", resposta custom digitada).
  bot.callbackQuery(/^perm:(.+):(once|always|reject)$/, async (ctx) => {
    const m = ctx.match as RegExpMatchArray;
    await ctx.answerCallbackQuery("respondido");
    await replyPermission(bot, ctx.chat!.id, m[1], m[2] as "once" | "always" | "reject");
  });
  bot.callbackQuery(/^qo:(.+):(\d+):(\d+)$/, async (ctx) => {
    const m = ctx.match as RegExpMatchArray;
    await ctx.answerCallbackQuery("ok");
    await chooseOption(bot, ctx.chat!.id, m[1], Number(m[2]), Number(m[3]));
  });
  bot.callbackQuery(/^qt:(.+):(\d+):(\d+)$/, async (ctx) => {
    const m = ctx.match as RegExpMatchArray;
    await ctx.answerCallbackQuery("alternado");
    await toggleOption(bot, ctx.chat!.id, m[1], Number(m[2]), Number(m[3]));
  });
  bot.callbackQuery(/^qs:(.+)$/, async (ctx) => {
    const m = ctx.match as RegExpMatchArray;
    await ctx.answerCallbackQuery("enviando");
    await submitQuestions(bot, ctx.chat!.id, m[1]);
  });
  bot.callbackQuery(/^qr:(.+)$/, async (ctx) => {
    const m = ctx.match as RegExpMatchArray;
    await ctx.answerCallbackQuery("rejeitado");
    await rejectQuestions(bot, ctx.chat!.id, m[1], rejectForm);
  });
  bot.callbackQuery(/^qc:(.+):(\d+)$/, async (ctx) => {
    const m = ctx.match as RegExpMatchArray;
    await ctx.answerCallbackQuery();
    await askCustom(bot, ctx.chat!.id, m[1], Number(m[2]));
  });

  bot.on("message:text", async (ctx) => {
    const text = ctx.message.text.trim();
    const chatId = ctx.chat.id;
    if (await consumeCustomAnswer(bot, chatId, text)) return;
    const m = text.match(/^(allow|deny):(\S+)\s*(once|always)?$/i);
    const sid = cfgFor(chatId).sid;
    if (m && sid) {
      const [, verb, id] = m;
      await answerPermission(sid, id, verb.toLowerCase() === "allow" ? "once" : "reject");
      await ctx.reply("✅ respondido.");
      return;
    }
    const f = text.match(/^form:(\S+)\s+(\{.*\})$/s);
    if (f && sid) {
      try {
        await answerForm(sid, f[1], JSON.parse(f[2]) as Record<string, unknown>);
        await ctx.reply("✅ form respondido.");
      } catch {
        await ctx.reply("❌ form inválido. Use: form:<id> {\"campo\": valor}");
      }
      return;
    }
    const target = await ensureSid(chatId);
    if (!target) {
      await ctx.reply("❌ sem sessão (servidor fora?).");
      return;
    }
    const cfg = cfgFor(chatId);
    const turn = await startTurn(bot, chatId, target);
    if (!turn) return; // já há turno rodando (startTurn avisou)
    try {
      await sendMessage(target, text, { model: cfg.model, agent: cfg.agent });
    } catch (e) {
      await ctx.reply(`❌ falha ao enviar: ${e}`.slice(0, 500));
      const live = getTurn(chatId);
      if (live) {
        const { finishTurn } = await import("./turns.ts");
        (live.state as Record<string, unknown>).out_text = `❌ Falha ao enviar para o opencode: ${e}`;
        await finishTurn(bot, live);
      }
    }
  });

  return bot;
}

async function batteryWatch(bot: Bot): Promise<void> {
  await Bun.sleep(10_000);
  let lastStatus: string | null = null;
  let fullNotified = false;
  const { BATTERY_CHECK_INTERVAL } = await import("./config.ts");
  for (;;) {
    try {
      const b = await batteryOnce();
      const pct = b.capacity !== null ? parseInt(b.capacity, 10) : NaN;
      const status = b.status ?? "Unknown";
      const chatId = ownerChatId();
      if (lastStatus !== null && lastStatus !== status) {
        if (status === "Charging") await bot.api.sendMessage(chatId, `🔌 *Carregador conectado* (${b.capacity}%).`, { parse_mode: "Markdown" });
        else if (status === "Discharging") await bot.api.sendMessage(chatId, `🔋 *Na bateria* (${b.capacity}%).`, { parse_mode: "Markdown" });
      }
      if (status === "Full" && !fullNotified) {
        fullNotified = true;
        await bot.api.sendMessage(chatId, "🔋 *Carga completa!*", { parse_mode: "Markdown" });
      } else if (status !== "Full") {
        fullNotified = false;
      }
      if (Number.isFinite(pct) && pct <= BATTERY_LOW_PCT && status !== "Charging" && status !== "Full") {
        await bot.api.sendMessage(chatId, `⚠️ *Bateria baixa!* ${pct}% restante.`, { parse_mode: "Markdown" });
      }
      lastStatus = status;
    } catch (e) {
      console.warn("battery watch:", e);
    }
    await Bun.sleep(BATTERY_CHECK_INTERVAL * 1000);
  }
}

export async function bootstrap(): Promise<Bot> {
  if (!storeReady) {
    try {
      for (const [id, cfg] of loadChats()) chats.set(id, cfg);
    } catch (e) {
      console.warn("store: partindo de chats vazios:", e);
    }
    storeReady = true;
  }
  await ensureServer();
  sessions = await listSessions();
  const bot = createBot();
  void consumeEvents((ev) => void routeEvent(bot, ev), () => stopSse);
  void batteryWatch(bot).catch((e) => console.warn("batteryWatch saiu:", e));
  return bot;
}

export function shutdown(): void {
  stopSse = true;
}

export { OC_PORT, OC_URL };
