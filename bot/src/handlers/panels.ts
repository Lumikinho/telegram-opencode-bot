/** Painel único: toda tela edita o mesmo balão (transição sem flood). */
import { InlineKeyboard, type Api } from "grammy";
import { MODELS_PER_PAGE, baseModelSpec, clampModelPage, formatModelRef, modelsHead, modelsKeyboardRows, modelsPages, splitModelRef } from "../utils/models.ts";
import { cfgFor } from "../services/store.ts";
import { listAgents, listModels, listSessions, modelVariants, serverInfo } from "../services/opencode.ts";
import { batteryOnce, funnelStatus, renderMarkdown } from "../services/python.ts";
import { EXEC_HELP } from "../services/exec.ts";
import { escHtml, restartConfirmKeyboard } from "../services/restart.ts";

export interface Screen {
  html: string;
  keyboard: InlineKeyboard;
}

/** Mostra a tela editando o balão do painel; se sumiu, manda nova. */
export async function showPanel(api: Api, chatId: number, html: string, keyboard?: InlineKeyboard): Promise<void> {
  const cfg = cfgFor(chatId);
  if (cfg.panelId !== undefined) {
    try {
      await api.editMessageText(chatId, cfg.panelId, html, { parse_mode: "HTML", reply_markup: keyboard });
      return;
    } catch (e) {
      if (/not modified/i.test(String(e))) return;
    }
  }
  try {
    const msg = await api.sendMessage(chatId, html, { parse_mode: "HTML", reply_markup: keyboard });
    cfg.panelId = msg.message_id;
  } catch (e) {
    console.warn("painel: falha ao enviar:", (e as Error)?.message ?? e);
  }
}

export function backTo(data: string): InlineKeyboard {
  return new InlineKeyboard().text("[<] Voltar", data);
}

export async function screenMain(chatId: number): Promise<Screen> {
  const info = await serverInfo();
  const cfg = cfgFor(chatId);
  const dot = info.ok ? "[ON]" : "[OFF]";
  const sid = cfg.sid ? `<code>${escHtml(cfg.sid.slice(-6))}</code>` : "<i>—</i>";
  const html =
    `[BOT] <b>Painel</b>  ${dot} ${escHtml(info.status)}\n` +
    `sessão ${sid} · modelo <code>${escHtml(cfg.model ?? "padrão")}</code> · agente <code>${escHtml(cfg.agent ?? "padrão")}</code>`;
  const keyboard = new InlineKeyboard()
    .text("[BOT] Opencode", "p:opencode").text("[SRV] Servidor", "p:server").row()
    .text("[NEW] Nova conversa", "menu:new").text("[?] Ajuda", "p:help");
  return { html, keyboard };
}

export function screenOpencode(chatId: number): Screen {
  const cfg = cfgFor(chatId);
  const sid = cfg.sid ? `<code>${escHtml(cfg.sid.slice(-6))}</code>` : "<i>—</i>";
  const html =
    `[BOT] <b>Opencode</b>\nsessão ${sid}\n` +
    `modelo <code>${escHtml(cfg.model ?? "padrão")}</code>\n` +
    `agente <code>${escHtml(cfg.agent ?? "padrão")}</code>`;
  const keyboard = new InlineKeyboard()
    .text("[NEW] Nova conversa", "menu:new").text("[DOCS] Sessões", "p:sessions").row()
    .text("[CFG] Modelos", "p:models").text("[DEV] Agentes", "p:agents").row()
    .text("[<] Voltar", "p:main");
  return { html, keyboard };
}

export async function screenServer(): Promise<Screen> {
  const info = await serverInfo();
  const dot = info.ok ? "[ON]" : "[OFF]";
  const lat = info.latencyMs !== null ? ` (${info.latencyMs}ms)` : "";
  const html =
    `[SRV] <b>Servidor</b>  ${dot} ${escHtml(info.status)}${lat}\n` +
    `<code>${escHtml(info.url)}</code>` +
    (info.error ? `\n[ERR] <code>${escHtml(info.error).slice(0, 200)}</code>` : "");
  const keyboard = new InlineKeyboard()
    .text("[PWR] Funnel", "p:funnel").text("[BAT] Bateria", "p:battery").row()
    .text("[REPEAT] Reiniciar", "p:restart").text("[RELOAD] Atualizar", "p:server").row()
    .text("[<] Voltar", "p:main");
  return { html, keyboard };
}

export function screenHelp(): Screen {
  const html =
    `[?] <b>Ajuda</b>\n\n` +
    `<code>/menu</code> painel · <code>/new</code> nova conversa · <code>/cancel</code> interrompe\n` +
    `<code>/status</code> servidor · <code>/bateria</code> bateria · <code>/funnel</code> funnel\n` +
    `<code>/models</code> <code>/agents</code> <code>/sessions</code> <code>/restart</code>\n` +
    `${escHtml(EXEC_HELP)}\n\n` +
    `<i>Mande texto para conversar, anexo com legenda, ou botão abaixo.</i>`;
  return { html, keyboard: backTo("p:main") };
}

export async function screenModels(chatId: number, page: number): Promise<Screen> {
  const models = (await listModels()).filter((m) => m.includes("/"));
  const cfg = cfgFor(chatId);
  if (!models.length) {
    return { html: "[CFG] <b>Modelos</b>\n(i) Nenhum modelo listado pelo servidor.", keyboard: backTo("p:opencode") };
  }
  const total = modelsPages(models.length, MODELS_PER_PAGE);
  const p = clampModelPage(page, total);
  cfg.modelsPage = p;
  const html = modelsHead(cfg.model, p, total);
  const keyboard = buttonGrid([...modelsKeyboardRows(models, p, MODELS_PER_PAGE), [{ text: "[<] Voltar", data: "p:opencode" }]]);
  return { html, keyboard };
}

export async function screenVariants(chatId: number, spec: string): Promise<Screen> {
  const base = baseModelSpec(spec);
  const variants = await modelVariants(base);
  const cfg = cfgFor(chatId);
  cfg.pendingModel = base;
  const cur = cfg.model ?? "padrão";
  if (!variants.length) {
    cfg.model = base;
    cfg.pendingModel = undefined;
    const s = await screenModels(chatId, cfg.modelsPage ?? 0);
    return { html: `[OK] Modelo definido: <code>${escHtml(base)}</code>\n\n${s.html}`, keyboard: s.keyboard };
  }
  const html =
    `[CFG] <b>Variante</b> de <code>${escHtml(base)}</code>\n` +
    `atual: <code>${escHtml(cur)}</code>\nToque para trocar:`;
  const keyboard = new InlineKeyboard();
  for (const v of variants.slice(0, 12)) {
    const full = formatModelRef({ ...splitModelRef(base), variant: v });
    keyboard.text(`${cfg.model === full ? "* " : ""}${v}`.slice(0, 40), `var:${v}`.slice(0, 64)).row();
  }
  keyboard.text(`${cur === base ? "* " : ""}Padrão`.slice(0, 40), "var:-").row();
  keyboard.text("[<] Voltar", "p:models");
  return { html, keyboard };
}

export async function screenAgents(chatId: number): Promise<Screen> {
  const agents = await listAgents();
  const cur = cfgFor(chatId).agent;
  if (!agents.length) {
    return { html: "[DEV] <b>Agentes</b>\n(i) Nenhum agente listado pelo servidor.", keyboard: backTo("p:opencode") };
  }
  const html = `[DEV] <b>Agentes</b>\natual: <code>${escHtml(cur ?? "padrão")}</code>\nToque para trocar:`;
  const keyboard = new InlineKeyboard();
  for (const a of agents.slice(0, 20)) {
    keyboard.text(`${a === cur ? "* " : ""}${a}`.slice(0, 40), `ag:${a}`.slice(0, 64)).row();
  }
  keyboard.text("[<] Voltar", "p:opencode");
  return { html, keyboard };
}

export async function screenSessions(chatId: number): Promise<Screen> {
  const sessions = await listSessions();
  const cur = cfgFor(chatId).sid;
  if (!sessions.length) {
    return { html: "[DOCS] <b>Sessões</b>\n(i) Nenhuma sessão no servidor.", keyboard: backTo("p:opencode") };
  }
  const html = `[DOCS] <b>Sessões</b> (${sessions.length})\natual: <code>${escHtml(cur?.slice(-6) ?? "—")}</code>\nToque para retomar:`;
  const keyboard = new InlineKeyboard();
  for (const s of sessions.slice(0, 10)) {
    if (`ses:${s.id}`.length > 60) continue;
    keyboard.text(`${s.id === cur ? "* " : ""}…${s.id.slice(-6)}`, `ses:${s.id}`).row();
  }
  keyboard.text("[<] Voltar", "p:opencode");
  return { html, keyboard };
}

export async function screenBattery(): Promise<Screen> {
  let html: string;
  try {
    const b = await batteryOnce();
    html = await renderMarkdown(b.formatted);
  } catch (e) {
    html = `[ERR] bateria indisponível: <code>${escHtml(String(e)).slice(0, 200)}</code>`;
  }
  const keyboard = new InlineKeyboard()
    .text("[RELOAD] Atualizar", "p:battery").text("[<] Voltar", "p:server");
  return { html, keyboard };
}

export async function screenFunnel(): Promise<Screen> {
  let html: string;
  try {
    const s = await funnelStatus();
    const act = (s.funnels ?? []).filter((f) => f.on);
    const lines = [`[PWR] <b>Funnel</b> (${act.length} ativo(s))`];
    for (const f of (s.funnels ?? []).slice(0, 8)) {
      lines.push(`${f.on ? "[ON]" : "[OFF]"} <code>${escHtml(f.url)}</code>`);
      for (const m of (f.mappings ?? []).slice(0, 3)) lines.push(`  -&gt; <code>${escHtml(m)}</code>`);
    }
    if (!(s.funnels ?? []).length) lines.push("<i>Nenhum funnel ativo.</i>");
    html = lines.join("\n");
  } catch (e) {
    html = `[ERR] funnel: <code>${escHtml(String(e)).slice(0, 200)}</code>`;
  }
  const keyboard = new InlineKeyboard()
    .text("[PWR] Ligar", "menu:funnel-on").text("[PWR] Desligar", "menu:funnel-off").row()
    .text("[RELOAD] Atualizar", "p:funnel").text("[<] Voltar", "p:server");
  return { html, keyboard };
}

export function screenRestart(): Screen {
  const html = "[!] <b>Reiniciar o quê?</b>\n\nRespostas em andamento serão interrompidas.";
  const keyboard = buttonGrid([...restartConfirmKeyboard(), [{ text: "[<] Voltar", data: "p:server" }]]);
  return { html, keyboard };
}

export function buttonGrid(rows: { text: string; data: string }[][]): InlineKeyboard {
  let kb = new InlineKeyboard();
  rows.forEach((row, ri) => {
    row.forEach((b) => {
      kb = kb.text(b.text, b.data);
    });
    if (ri < rows.length - 1) kb = kb.row();
  });
  return kb;
}
