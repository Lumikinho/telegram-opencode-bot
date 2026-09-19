/** Estado do gateway: chats (sid/model/agent) em memória + SQLite.
 *
 * Sem persistência em disco os sids morrem a cada boot; aqui o SQLite
 * guarda sid/model/agent por chat e o restore valida contra o servidor.
 */
import { loadChats, saveChat } from "../db/index.ts";
import { listSessions, restoreChatSession, type SessionSummary } from "./opencode.ts";

export interface ChatCfg {
  sid?: string | null;
  model?: string;
  agent?: string;
  /** Mensagem do painel: todas as telas editam este mesmo balão. */
  panelId?: number;
  modelsPage?: number;
  /** Modelo aguardando escolha de variante (fluxo mod: -> var:). */
  pendingModel?: string;
}

const chats = new Map<number, ChatCfg>();
let sessions: SessionSummary[] = [];

/** SIDs criados neste boot. O restore só readota sessão que o próprio bot
 * criou — impede anexar a sessão viva do TUI/CLI do dono. */
const bootSids = new Set<string>();

export function cfgFor(chatId: number): ChatCfg {
  let c = chats.get(chatId);
  if (!c) {
    c = {};
    chats.set(chatId, c);
  }
  return c;
}

export function rememberSid(sid: string): void {
  if (sid) bootSids.add(sid);
}

export function bootSidSet(): Set<string> {
  return bootSids;
}

export function getSessions(): SessionSummary[] {
  return sessions;
}

export function setSessions(next: SessionSummary[]): void {
  sessions = next;
}

/** Persiste sid/model/agent do chat no SQLite. */
export function persistChat(chatId: number): void {
  const c = chats.get(chatId);
  if (!c) return;
  saveChat({ chat_id: chatId, sid: c.sid ?? null, model: c.model ?? null, agent: c.agent ?? null });
  if (c.sid) bootSids.add(c.sid);
}

/** Carrega chats do SQLite (chamado no bootstrap, antes do restore). */
export function restoreChats(): void {
  for (const row of loadChats()) {
    chats.set(row.chat_id, {
      sid: row.sid,
      model: row.model ?? undefined,
      agent: row.agent ?? undefined,
    });
    if (row.sid) bootSids.add(row.sid);
  }
}

/** Registra sessão nova criada neste boot. */
export function addSession(sid: string): void {
  sessions.push({ id: sid, time: { updated: 0 } });
}

/** Derruba sids guardados que sumiram do servidor. */
export function dropDeadSids(): void {
  const live = new Set(sessions.map((s) => s.id));
  for (const [chatId, cfg] of chats) {
    if (cfg.sid && !live.has(cfg.sid)) {
      bootSids.delete(cfg.sid);
      cfg.sid = null;
      persistChat(chatId);
    }
  }
}

/** Garante uma sessão opencode para o chat (cria se preciso). */
export async function ensureSid(chatId: number): Promise<string | null> {
  const cfg = cfgFor(chatId);
  if (cfg.sid) return cfg.sid;
  const { sid } = await restoreChatSession(cfg, sessions, bootSids);
  if (sid) {
    rememberSid(sid);
    persistChat(chatId);
  }
  return sid;
}

/** Servidor novo = sessões antigas mortas: refresca a lista e derruba
 * os sids guardados que sumiram, senão a próxima mensagem dá 404. */
export async function refreshSessionsAfterCycle(): Promise<void> {
  sessions = await listSessions();
  dropDeadSids();
}
