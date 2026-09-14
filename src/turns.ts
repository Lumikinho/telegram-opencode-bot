/** Ciclo de turnos no gateway: estado de protocolo no worker Python,
 * I/O Telegram aqui (status, balão de resposta, think, typing).
 *
 * Espelha `bot/turns.py`: placeholder "pensando…", edição com throttle de
 * 1s, balão de resultado por streaming (STREAM_MIN=150), e no fim a ordem
 * invertida resposta-em-cima / think-embaixo + botões pós-turno.
 */
import { InlineKeyboard, type Bot } from "grammy";
import { OPENCODE_DIR } from "./config.ts";
import { answerForm, answerPermission, type ServerEvent } from "./opencode.ts";
import {
  turnAnswerCustom,
  turnDropForm,
  turnFold,
  turnNew,
  turnRenderResult,
  turnRenderRunning,
  turnRenderThink,
  turnSelectOption,
  turnSetCustom,
  turnSplit,
  turnSubmitForm,
  turnTelegramHtml,
  type TurnButton,
  type TurnState,
} from "./workers.ts";

const STREAM_MIN = 150;

export interface LiveTurn {
  state: TurnState;
  chatId: number;
  startedAt: number;
  lastPush: number;
  statusMsgId: number | null;
  resultMsgId: number | null;
  resultLast: string;
  typingTimer: ReturnType<typeof setInterval> | null;
  streamTimer: ReturnType<typeof setInterval> | null;
}

const byChat = new Map<number, LiveTurn>();
const bySid = new Map<string, number>();

export function getTurn(chatId: number): LiveTurn | undefined {
  return byChat.get(chatId);
}

function sidOf(ev: ServerEvent): string | null {
  const d = (ev.data ?? {}) as Record<string, unknown>;
  if (typeof d.sessionID === "string") return d.sessionID;
  const form = d.form as Record<string, unknown> | undefined;
  if (form && typeof form.sessionID === "string") return form.sessionID;
  return null;
}

function toKeyboard(rows: TurnButton[][]): InlineKeyboard | undefined {
  if (!rows.length) return undefined;
  let kb = new InlineKeyboard();
  rows.forEach((row, ri) => {
    row.forEach((b) => {
      kb = kb.text(b.text.slice(0, 40), b.data.slice(0, 64));
    });
    if (ri < rows.length - 1) kb = kb.row();
  });
  return kb;
}

function afterTurnKeyboard(): InlineKeyboard {
  return new InlineKeyboard()
    .text("Nova conversa", "/new")
    .text("Ajuda", "/help")
    .text("Status", "/status");
}

function startTyping(bot: Bot, t: LiveTurn): void {
  stopTyping(t);
  t.typingTimer = setInterval(() => {
    bot.api.sendChatAction(t.chatId, "typing").catch(() => {});
  }, 4000);
  bot.api.sendChatAction(t.chatId, "typing").catch(() => {});
}

function stopTyping(t: LiveTurn): void {
  if (t.typingTimer) clearInterval(t.typingTimer);
  t.typingTimer = null;
}

function stopStream(t: LiveTurn): void {
  if (t.streamTimer) clearInterval(t.streamTimer);
  t.streamTimer = null;
}

export async function startTurn(bot: Bot, chatId: number, sid: string, placeholderText = "💭 *opencode pensando…*"): Promise<LiveTurn | null> {
  if (byChat.has(chatId)) {
    await bot.api.sendMessage(chatId, "⏳ Ainda estou processando a mensagem anterior…");
    return null;
  }
  const state = await turnNew(chatId);
  (state as Record<string, unknown>).sid = sid;
  const t: LiveTurn = {
    state,
    chatId,
    startedAt: Date.now(),
    lastPush: 0,
    statusMsgId: null,
    resultMsgId: null,
    resultLast: "",
    typingTimer: null,
    streamTimer: null,
  };
  byChat.set(chatId, t);
  bySid.set(sid, chatId);
  try {
    const msg = await bot.api.sendMessage(chatId, placeholderText, { parse_mode: "Markdown" });
    t.statusMsgId = msg.message_id;
  } catch {
    /* placeholder é melhor-esforço; finish cria fallback */
  }
  startTyping(bot, t);
  t.streamTimer = setInterval(() => {
    void flushStream(bot, t);
  }, 800);
  return t;
}

export async function pushStatus(bot: Bot, t: LiveTurn, force = false): Promise<void> {
  const now = Date.now();
  if (!force && now - t.lastPush < 1000) return;
  t.lastPush = now;
  const st = t.state as Record<string, unknown>;
  if (st.done) {
    const text = await turnRenderThink(t.state, (Date.now() - t.startedAt) / 1000, OPENCODE_DIR);
    if (t.statusMsgId !== null) {
      await bot.api.editMessageText(t.chatId, t.statusMsgId, text, { parse_mode: "HTML" }).catch(() => {});
    }
    return;
  }
  const { text, keyboard } = await turnRenderRunning(t.state, OPENCODE_DIR);
  if (t.statusMsgId === null) return;
  const kb = toKeyboard(keyboard);
  try {
    await bot.api.editMessageText(t.chatId, t.statusMsgId, text || "⏳ *pensando…*", {
      parse_mode: "Markdown",
      reply_markup: kb,
    });
  } catch {
    // parse_mode ou botão rejeitado: tenta sem formatação (port de _safe_edit_message).
    try {
      await bot.api.editMessageText(t.chatId, t.statusMsgId, text || "pensando…", { reply_markup: kb });
    } catch {
      /* status parado é melhor que crash */
    }
  }
}

async function flushStream(bot: Bot, t: LiveTurn, force = false): Promise<void> {
  const text = await turnRenderResult(t.state);
  if (text === t.resultLast) return;
  const raw = String((t.state as Record<string, unknown>).out_text ?? "");
  if (t.resultMsgId === null && !force && raw.trim().length < STREAM_MIN) return;
  try {
    if (t.resultMsgId === null) {
      const msg = await bot.api.sendMessage(t.chatId, text, { parse_mode: "HTML" });
      t.resultMsgId = msg.message_id;
    } else {
      await bot.api.editMessageText(t.chatId, t.resultMsgId, text, { parse_mode: "HTML" });
    }
    t.resultLast = text;
  } catch {
    /* streaming é melhor-esforço */
  }
}

export async function finishTurn(bot: Bot, t: LiveTurn): Promise<void> {
  if (!byChat.has(t.chatId)) return; // idempotente (vários eventos de fim)
  byChat.delete(t.chatId);
  const sid = String((t.state as Record<string, unknown>).sid ?? "");
  if (sid && bySid.get(sid) === t.chatId) bySid.delete(sid);
  (t.state as Record<string, unknown>).done = true;
  stopTyping(t);
  stopStream(t);
  await flushStream(bot, t, true);
  const elapsed = (Date.now() - t.startedAt) / 1000;

  const raw = String((t.state as Record<string, unknown>).out_text ?? "").trim() || "(sem resposta)";
  const chunks = await turnSplit(raw, 3500);
  const bodies: string[] = [];
  for (const c of chunks) bodies.push(await turnTelegramHtml(c, 3500));
  const first = bodies[0];

  let answerId: number | null = t.statusMsgId;
  if (answerId !== null) {
    try {
      await bot.api.editMessageText(t.chatId, answerId, first, { parse_mode: "HTML" });
    } catch {
      answerId = null;
    }
  }
  if (answerId === null) {
    try {
      const msg = await bot.api.sendMessage(t.chatId, first, { parse_mode: "HTML" });
      answerId = msg.message_id;
    } catch {
      answerId = null;
    }
  }
  let lastId = answerId;
  for (const body of bodies.slice(1)) {
    try {
      const msg = await bot.api.sendMessage(t.chatId, body, { parse_mode: "HTML" });
      lastId = msg.message_id;
    } catch {
      /* segue */
    }
  }

  const think = await turnRenderThink(t.state, elapsed, OPENCODE_DIR);
  let thinkId = t.resultMsgId;
  if (thinkId !== null && thinkId !== answerId && bodies.length > 1) {
    await bot.api.deleteMessage(t.chatId, thinkId).catch(() => {});
    thinkId = null;
  }
  let placed = false;
  if (thinkId !== null && thinkId !== answerId) {
    try {
      await bot.api.editMessageText(t.chatId, thinkId, think, { parse_mode: "HTML" });
      placed = true;
    } catch {
      placed = false;
    }
  }
  if (!placed) {
    await bot.api.sendMessage(t.chatId, think, { parse_mode: "HTML" }).catch(() => {});
  }
  if (lastId !== null) {
    await bot.api.editMessageReplyMarkup(t.chatId, lastId, { reply_markup: afterTurnKeyboard() }).catch(() => {});
  }
}

/** Roteia um evento SSE v2 para o turno da sessão (port de _dispatch). */
export async function routeEvent(bot: Bot, ev: ServerEvent): Promise<void> {
  const sid = sidOf(ev);
  const chatId = sid ? bySid.get(sid) : undefined;
  if (chatId === undefined) return;
  const t = byChat.get(chatId);
  if (!t) return;
  let res;
  try {
    res = await turnFold(t.state, ev);
  } catch (e) {
    console.warn("fold falhou:", e);
    return;
  }
  t.state = res.turn;
  if (res.action === "finish") {
    await finishTurn(bot, t);
  } else if (res.action === "push_force") {
    await pushStatus(bot, t, true);
  } else if (res.action === "push") {
    await pushStatus(bot, t, false);
  }
}

export async function cancelTurn(bot: Bot, chatId: number): Promise<boolean> {
  const t = byChat.get(chatId);
  if (!t) return false;
  await finishTurn(bot, t);
  return true;
}

/** Interrompe todos os turnos (port de _kill_all_turns). */
export async function killAllTurns(bot: Bot, chatId?: number): Promise<void> {
  const targets = chatId !== undefined ? [chatId] : [...byChat.keys()];
  for (const cid of targets) {
    const t = byChat.get(cid);
    if (!t) continue;
    const sid = String((t.state as Record<string, unknown>).sid ?? "");
    if (sid) {
      const { interruptSession } = await import("./opencode.ts");
      await interruptSession(sid);
    }
    await finishTurn(bot, t);
  }
}

// ---- interações de permissão / perguntas (callbacks) ----

export async function replyPermission(bot: Bot, chatId: number, permId: string, reply: "once" | "always" | "reject"): Promise<void> {
  const t = byChat.get(chatId);
  const sid = t ? String((t.state as Record<string, unknown>).sid ?? "") : null;
  if (!t || !sid) {
    await bot.api.sendMessage(chatId, "❌ sem turno ativo.");
    return;
  }
  await answerPermission(sid, permId, reply);
  await pushStatus(bot, t, true);
}

export async function chooseOption(bot: Bot, chatId: number, rid: string, qidx: number, opt: number): Promise<void> {
  const t = byChat.get(chatId);
  if (!t) return;
  const r = await turnSelectOption(t.state, rid, qidx, opt);
  t.state = r.turn;
  // Seleção simples responde na hora (port de callbacks.py qo:).
  const st = t.state as Record<string, unknown>;
  const questions = (st.questions as Record<string, unknown>[]) ?? [];
  const item = questions.find((q) => q.request_id === rid && q.qidx === qidx) as
    | { multiple?: boolean; options?: { value?: unknown }[] }
    | undefined;
  if (item && !item.multiple) {
    const sid = String(st.sid ?? "");
    const sub = await turnSubmitForm(t.state, rid);
    t.state = sub.turn;
    if (sub.complete && sid) await answerForm(sid, rid, sub.answer);
  }
  await pushStatus(bot, t, true);
}

export async function toggleOption(bot: Bot, chatId: number, rid: string, qidx: number, opt: number): Promise<void> {
  const t = byChat.get(chatId);
  if (!t) return;
  const r = await turnSelectOption(t.state, rid, qidx, opt);
  t.state = r.turn;
  await pushStatus(bot, t, true);
}

export async function submitQuestions(bot: Bot, chatId: number, rid: string): Promise<void> {
  const t = byChat.get(chatId);
  if (!t) return;
  const sub = await turnSubmitForm(t.state, rid);
  t.state = sub.turn;
  if (!sub.complete) {
    await bot.api.sendMessage(chatId, "❓ Falta responder algum campo.");
    await pushStatus(bot, t, true);
    return;
  }
  const sid = String((t.state as Record<string, unknown>).sid ?? "");
  const ok = sid ? await answerForm(sid, rid, sub.answer) : false;
  await bot.api.sendMessage(chatId, ok ? "✅ *Resposta enviada ao opencode.*" : "❌ *Falha ao enviar resposta.*", {
    parse_mode: "Markdown",
  });
  await pushStatus(bot, t, true);
}

export async function rejectQuestions(bot: Bot, chatId: number, rid: string, rejectFn: (sid: string, rid: string) => Promise<boolean>): Promise<void> {
  const t = byChat.get(chatId);
  if (!t) return;
  const sid = String((t.state as Record<string, unknown>).sid ?? "");
  if (sid) await rejectFn(sid, rid);
  t.state = await turnDropForm(t.state, rid);
  await pushStatus(bot, t, true);
}

export async function askCustom(bot: Bot, chatId: number, rid: string, qidx: number): Promise<void> {
  const t = byChat.get(chatId);
  if (!t) return;
  t.state = await turnSetCustom(t.state, rid, qidx);
  await bot.api.sendMessage(chatId, "✏️ Digite sua resposta:");
}

/** Roteia texto livre para resposta custom pendente (port de _consume_custom_answer). */
export async function consumeCustomAnswer(bot: Bot, chatId: number, text: string): Promise<boolean> {
  const t = byChat.get(chatId);
  if (!t) return false;
  const st = t.state as Record<string, unknown>;
  const awaiting = st.awaiting_custom as [string, number] | null;
  if (!awaiting) return false;
  const [rid, qi] = awaiting;
  const r = await turnAnswerCustom(t.state, rid, qi, text);
  t.state = r.turn;
  const sid = String((t.state as Record<string, unknown>).sid ?? "");
  const ok = r.complete && sid ? await answerForm(sid, rid, r.answer) : false;
  await bot.api.sendMessage(chatId, ok ? "✅ *Resposta enviada ao opencode.*" : "❌ *Falha ao enviar resposta.*", {
    parse_mode: "Markdown",
  });
  await pushStatus(bot, t, true);
  return true;
}
