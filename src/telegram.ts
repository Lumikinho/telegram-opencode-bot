/** Bot Telegram (grammy): auth do dono, comandos e texto livre -> opencode v2. */
import { Bot, InlineKeyboard } from "grammy";
import {
  BOT_TOKEN,
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
  type SessionSummary,
} from "./opencode.ts";
import { batteryOnce, funnelOff, funnelOn, funnelStatus, turnTelegramHtml } from "./workers.ts";
import { batteryWatch } from "./battery.ts";
import { EXEC_HELP, cancelExecs, formatExecResult, runExec } from "./exec.ts";
import { loadChats, saveChat } from "./store.ts";
import { getStartupInfo, startupMarkdown } from "./version.ts";
import { captionOf, collectMedia, downloadParts } from "./media.ts";
import { RESTART_LABELS, parseRestartTarget, performRestart, restartConfirmKeyboard } from "./restart.ts";
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

function buttonGrid(rows: { text: string; data: string }[][]): InlineKeyboard {
  let kb = new InlineKeyboard();
  rows.forEach((row, ri) => {
    row.forEach((b) => {
      kb = kb.text(b.text, b.data);
    });
    if (ri < rows.length - 1) kb = kb.row();
  });
  return kb;
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
    "opencode bot híbrido (Bun+TS+Python) online.\nUse /menu, /new, /status, /bateria, /funnel, /exec.",
  ));
  bot.command("help", (ctx) => ctx.reply(
    "/menu /new /cancel /status /bateria /funnel /models /agents /sessions /restart\n" + EXEC_HELP,
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
    const killedExec = await cancelExecs(ctx.chat.id);
    const had = await cancelTurn(bot, ctx.chat.id);
    await ctx.reply(killedExec || had ? "⏹ Interrompido (turno/exec)." : "⏹ Nada em andamento.");
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

  async function handleExec(ctx: { match?: unknown; chat: { id: number }; reply: (t: string, o?: Record<string, unknown>) => Promise<unknown> }): Promise<void> {
    const raw = String((ctx.match as string | undefined) ?? "").trim();
    if (!raw) {
      await ctx.reply(`Uso: ${EXEC_HELP}\n\nSem shell: sem pipes/redirecionamentos. /cancel mata o exec.`);
      return;
    }
    const r = await runExec(raw, ctx.chat.id);
    await ctx.reply(formatExecResult(r), { parse_mode: "HTML" });
  }
  bot.command("exec", handleExec);
  bot.command("sh", handleExec);

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
    const arg = ctx.match?.toString().trim();
    if (arg) {
      const target = parseRestartTarget(arg);
      if (!target) {
        await ctx.reply("Uso: `/restart` ou `/restart bot|servidor|ambos`", { parse_mode: "Markdown" });
        return;
      }
      await performRestart(bot, ctx.chat.id, target);
      return;
    }
    await ctx.reply("⚠️ *Reiniciar o quê?*\n\nRespostas em andamento serão interrompidas.", {
      parse_mode: "Markdown",
      reply_markup: buttonGrid(restartConfirmKeyboard()),
    });
  });
  bot.callbackQuery(/^__restart:(bot|server|both)$/, async (ctx) => {
    const target = (ctx.match as RegExpMatchArray)[1] as "bot" | "server" | "both";
    await ctx.answerCallbackQuery(`reiniciando ${RESTART_LABELS[target]}`);
    try {
      await ctx.editMessageText(`🔁 *Reiniciando ${RESTART_LABELS[target]}...*`, { parse_mode: "Markdown" });
    } catch {
      /* segue para o restart mesmo sem editar */
    }
    await performRestart(bot, ctx.chat!.id, target);
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

  bot.callbackQuery("/status", async (ctx) => {
    const info = await serverInfo();
    await ctx.answerCallbackQuery(info.ok ? "servidor ativo" : "servidor parado");
    await ctx.reply(`${info.ok ? "🟢" : "🔴"} ${info.status} — ${info.url}`);
  });

  bot.on(
    [
      "message:photo",
      "message:document",
      "message:audio",
      "message:voice",
      "message:video",
      "message:video_note",
      "message:animation",
    ],
    async (ctx) => {
      const text = await captionOf(ctx);
      if (text && (await consumeCustomAnswer(bot, ctx.chat.id, text))) return;
      const entries = await collectMedia(ctx);
      if (!entries.length && !text) {
        await ctx.reply(
          "⚠️ Anexos suportados: foto, documento, áudio, voz, vídeo e GIF.\nEnvie junto um texto ou legenda com a instrução.",
        );
        return;
      }
      const target = await ensureSid(ctx.chat.id);
      if (!target) {
        await ctx.reply("❌ sem sessão (servidor fora?).");
        return;
      }
      const cfg = cfgFor(ctx.chat.id);
      const parts = entries.length ? await downloadParts(bot, entries) : [];
      const files = parts.filter((p) => p.type === "file");
      const notes = parts.filter((p) => p.type === "text").map((p) => (p as { text: string }).text);
      const prompt = [text, ...notes].filter(Boolean).join("\n");
      const turn = await startTurn(bot, ctx.chat.id, target);
      if (!turn) return;
      try {
        await sendMessage(target, prompt, {
          model: cfg.model,
          agent: cfg.agent,
          files: files as { type: "file"; url: string; filename?: string }[],
        });
      } catch (e) {
        await ctx.reply(`❌ falha ao enviar: ${e}`.slice(0, 500));
        const live = getTurn(ctx.chat.id);
        if (live) {
          const { finishTurn } = await import("./turns.ts");
          (live.state as Record<string, unknown>).out_text = `❌ Falha ao enviar para o opencode: ${e}`;
          await finishTurn(bot, live);
        }
      }
    },
  );

  return bot;
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
  void batteryWatch(bot, () => stopSse).catch((e) => console.warn("batteryWatch saiu:", e));
  await announceOnline(bot).catch((e) => console.warn("aviso de boot falhou:", e));
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
