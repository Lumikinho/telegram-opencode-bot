/** Callbacks inline: painel, modelos, agentes, sessões, permissões, forms, restart. */
import type { Bot } from "grammy";
import { escHtml, performRestart, RESTART_LABELS, type RestartHooks } from "../services/restart.ts";
import { createSession, listSessions, rejectForm } from "../services/opencode.ts";
import {
  addSession,
  bootSidSet,
  cfgFor,
  getSessions,
  persistChat,
  setSessions,
} from "../services/store.ts";
import {
  askCustom,
  cancelQueued,
  chooseOption,
  clearQueue,
  formatQueueHtml,
  getQueue,
  queueListKeyboard,
  rejectQuestions,
  replyPermission,
  submitQuestions,
  toggleOption,
} from "../services/turns.ts";
import {
  showPanel,
  screenAgents,
  screenBattery,
  screenFunnel,
  screenHelp,
  screenMain,
  screenModels,
  screenOpencode,
  screenRestart,
  screenServer,
  screenSessions,
  screenVariants,
  type Screen,
} from "./panels.ts";
import { funnelOff, funnelOn } from "../services/python.ts";
import { baseModelSpec, formatModelRef, splitModelRef } from "../utils/models.ts";

export function registerCallbacks(bot: Bot, hooks: RestartHooks): void {
  const show = (chatId: number, s: Screen) => showPanel(bot.api, chatId, s.html, s.keyboard);

  // Navegação do painel: uma tela por callback, tudo no mesmo balão.
  bot.callbackQuery(/^p:([a-z]+)(?::(\d+))?$/, async (ctx) => {
    const m = ctx.match as RegExpMatchArray;
    const where = m[1];
    const chatId = ctx.chat!.id;
    await ctx.answerCallbackQuery().catch(() => {});
    if (where === "main") await show(chatId, await screenMain(chatId));
    else if (where === "opencode") await show(chatId, screenOpencode(chatId));
    else if (where === "server") await show(chatId, await screenServer());
    else if (where === "help") await show(chatId, screenHelp());
    else if (where === "restart") await show(chatId, screenRestart());
    else if (where === "models") await show(chatId, await screenModels(chatId, Number(m[2] ?? cfgFor(chatId).modelsPage ?? 0)));
    else if (where === "variant") {
      const pending = cfgFor(chatId).pendingModel ?? baseModelSpec(cfgFor(chatId).model ?? "");
      if (pending.includes("/")) await show(chatId, await screenVariants(chatId, pending));
    }
    else if (where === "agents") await show(chatId, await screenAgents(chatId));
    else if (where === "sessions") await show(chatId, await screenSessions(chatId));
    else if (where === "battery") await show(chatId, await screenBattery());
    else if (where === "funnel") await show(chatId, await screenFunnel());
  });

  bot.callbackQuery("menu:new", async (ctx) => {
    const sid = await createSession();
    addSession(sid);
    cfgFor(ctx.chat!.id).sid = sid;
    persistChat(ctx.chat!.id);
    await ctx.answerCallbackQuery("sessão criada");
    const s = screenOpencode(ctx.chat!.id);
    await showPanel(bot.api, ctx.chat!.id, `[NEW] sessão criada: <code>${escHtml(sid.slice(-6))}</code>\n\n${s.html}`, s.keyboard);
  });
  bot.callbackQuery("menu:status", async (ctx) => {
    await ctx.answerCallbackQuery();
    await show(ctx.chat!.id, await screenServer());
  });
  bot.callbackQuery("menu:models", async (ctx) => {
    await ctx.answerCallbackQuery();
    await show(ctx.chat!.id, await screenModels(ctx.chat!.id, 0));
  });
  bot.callbackQuery("menu:agents", async (ctx) => {
    await ctx.answerCallbackQuery();
    await show(ctx.chat!.id, await screenAgents(ctx.chat!.id));
  });
  bot.callbackQuery(/^mod:(.+)$/, async (ctx) => {
    const spec = (ctx.match as RegExpMatchArray)[1];
    if (!spec.includes("/")) return;
    await ctx.answerCallbackQuery("carregando variantes");
    await show(ctx.chat!.id, await screenVariants(ctx.chat!.id, spec));
  });
  bot.callbackQuery(/^var:(.+)$/, async (ctx) => {
    const vid = (ctx.match as RegExpMatchArray)[1];
    const cfg = cfgFor(ctx.chat!.id);
    const base = cfg.pendingModel ?? baseModelSpec(cfg.model ?? "");
    if (!base.includes("/")) return;
    cfg.model = vid === "-" ? base : formatModelRef({ ...splitModelRef(base), variant: vid });
    cfg.pendingModel = undefined;
    persistChat(ctx.chat!.id);
    await ctx.answerCallbackQuery("modelo definido");
    await show(ctx.chat!.id, await screenModels(ctx.chat!.id, cfg.modelsPage ?? 0));
  });
  bot.callbackQuery(/^mpg:(\d+)$/, async (ctx) => {
    const asked = Number((ctx.match as RegExpMatchArray)[1]);
    await ctx.answerCallbackQuery().catch(() => {});
    await show(ctx.chat!.id, await screenModels(ctx.chat!.id, Number.isFinite(asked) ? asked : 0));
  });
  bot.callbackQuery(/^ag:(.+)$/, async (ctx) => {
    const name = (ctx.match as RegExpMatchArray)[1];
    if (!name) return;
    cfgFor(ctx.chat!.id).agent = name;
    persistChat(ctx.chat!.id);
    await ctx.answerCallbackQuery("agente definido");
    await show(ctx.chat!.id, await screenAgents(ctx.chat!.id));
  });
  bot.callbackQuery(/^ses:(.+)$/, async (ctx) => {
    const sid = (ctx.match as RegExpMatchArray)[1];
    setSessions(await listSessions());
    if (!getSessions().some((s) => s.id === sid)) {
      await ctx.answerCallbackQuery("sessão não existe mais");
      await show(ctx.chat!.id, await screenSessions(ctx.chat!.id));
      return;
    }
    bootSidSet().add(sid);
    cfgFor(ctx.chat!.id).sid = sid;
    persistChat(ctx.chat!.id);
    await ctx.answerCallbackQuery("sessão retomada");
    const s = screenOpencode(ctx.chat!.id);
    await show(ctx.chat!.id, s);
  });
  bot.callbackQuery("menu:funnel-on", async (ctx) => {
    await funnelOn().catch(() => ({ ok: false }));
    await ctx.answerCallbackQuery("funnel ligado");
    await show(ctx.chat!.id, await screenFunnel());
  });
  bot.callbackQuery("menu:funnel-off", async (ctx) => {
    await funnelOff().catch(() => ({ ok: false }));
    await ctx.answerCallbackQuery("funnel desligado");
    await show(ctx.chat!.id, await screenFunnel());
  });

  bot.callbackQuery(/^__restart:(bot|server|both)$/, async (ctx) => {
    const target = (ctx.match as RegExpMatchArray)[1] as "bot" | "server" | "both";
    await ctx.answerCallbackQuery(`reiniciando ${RESTART_LABELS[target]}`);
    try {
      await ctx.editMessageText(`[REPEAT] <b>Reiniciando ${escHtml(RESTART_LABELS[target])}...</b>`, { parse_mode: "HTML" });
    } catch {
      /* segue para o restart mesmo sem editar */
    }
    await performRestart(bot, ctx.chat!.id, target, hooks);
  });

  // Botões legados de comandos no painel.
  bot.callbackQuery("/status", async (ctx) => {
    await ctx.answerCallbackQuery().catch(() => {});
    await show(ctx.chat!.id, await screenServer());
  });

  // Permissões e forms v2 via botões.
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

  // Fila: cancelar um pedido ou limpar tudo.
  bot.callbackQuery(/^qcancel:(.+)$/, async (ctx) => {
    const qid = (ctx.match as RegExpMatchArray)[1];
    const ok = await cancelQueued(bot, ctx.chat!.id, qid);
    await ctx.answerCallbackQuery(ok ? "pedido cancelado" : "já saiu da fila").catch(() => {});
    try {
      await ctx.editMessageReplyMarkup({ reply_markup: queueListKeyboard(getQueue(ctx.chat!.id)) }).catch(() => {});
    } catch {
      /* melhor-esforço */
    }
  });
  bot.callbackQuery("qclear", async (ctx) => {
    const n = await clearQueue(bot, ctx.chat!.id);
    await ctx.answerCallbackQuery(n ? `${n} cancelado(s)` : "fila já vazia").catch(() => {});
    try {
      await ctx.editMessageText(formatQueueHtml(ctx.chat!.id), { parse_mode: "HTML" }).catch(() => {});
    } catch {
      /* melhor-esforço */
    }
  });
}
