/** Cliente HTTP do worker Python (FastAPI).
 *
 * Mesma API do antigo `workers.ts` (que usava `Bun.spawn`), agora via HTTP —
 * o contrato é o OpenAPI do worker (`src/types/openapi.ts`, gerado com
 * `bun run gen:types`). Para tarefas longas o bot não trava: cada chamada
 * tem timeout próprio e o streaming de status continua no gateway.
 */
import { WORKER_URL } from "../config/env.ts";
import type { components } from "../types/openapi.ts";

type S = components["schemas"];

async function post<T>(path: string, body: unknown, timeoutMs = 20_000): Promise<T> {
  return withSocketRetry(() => {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    return (async () => {
      try {
        const r = await fetch(`${WORKER_URL}${path}`, {
          method: "POST",
          signal: ctrl.signal,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body ?? {}),
        });
        if (!r.ok) throw new Error(`worker ${path} HTTP ${r.status}`);
        return (await r.json()) as T;
      } finally {
        clearTimeout(t);
      }
    })();
  });
}

async function get<T>(path: string, query = "", timeoutMs = 20_000): Promise<T> {
  return withSocketRetry(() => {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    return (async () => {
      try {
        const r = await fetch(`${WORKER_URL}${path}${query}`, { signal: ctrl.signal });
        if (!r.ok) throw new Error(`worker ${path} HTTP ${r.status}`);
        return (await r.json()) as T;
      } finally {
        clearTimeout(t);
      }
    })();
  });
}

/** Erro de socket fechado = transitório (restart do worker): tenta 1x de novo.
 * Timeout do nosso lado NÃO retenta (o worker pode ainda estar executando). */
function isSocketDrop(e: unknown): boolean {
  const s = e instanceof Error ? `${e.name}: ${e.message}` : String(e ?? "");
  return /socket .*closed|connection .*closed|ECONNRESET|terminated/i.test(s) && !/HTTP \d/.test(s);
}

async function withSocketRetry<T>(fn: () => Promise<T>): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    if (!isSocketDrop(e)) throw e;
    console.warn(`worker: socket caiu, retentando 1x (${e instanceof Error ? e.message : e})`);
    await Bun.sleep(1000);
    return fn();
  }
}

export interface BatteryInfo {
  capacity: string | null;
  status: string | null;
  health: string | null;
  technology: string | null;
  voltage_now: string | null;
  current_now: string | null;
  temp: string | null;
  formatted: string;
}

export async function batteryOnce(batteryPath?: string): Promise<BatteryInfo> {
  const q = batteryPath ? `?battery_path=${encodeURIComponent(batteryPath)}` : "";
  return get<BatteryInfo>("/api/battery", q);
}

export interface FunnelEntry {
  url: string;
  on: boolean;
  mappings: string[];
}

export async function funnelStatus(): Promise<{ raw: string; funnels: FunnelEntry[] }> {
  return get("/api/funnel/status");
}

export async function funnelOn(): Promise<{ ok: boolean; message: string }> {
  return post("/api/funnel/on", {}, 60_000);
}

export async function funnelOff(): Promise<{ ok: boolean; message: string }> {
  return post("/api/funnel/off", {}, 30_000);
}

export async function renderMarkdown(text: string): Promise<string> {
  const r = await post<S["RenderMarkdownResponse"]>("/api/render/markdown", { text });
  return r.html;
}

// ---- turnos ----

export type TurnState = Record<string, unknown>;

export type FoldAction = "none" | "push" | "push_force" | "finish";

export interface FoldResult {
  turn: TurnState;
  action: FoldAction;
  detail: {
    reason?: string;
    error?: string;
    tool_phase?: "started" | "ended";
    tool_id?: string;
    diff_path?: string;
  } | null;
}

export async function turnNew(chatId: number): Promise<TurnState> {
  const r = await post<S["NewTurnResponse"]>("/api/turns/new", { chat_id: chatId });
  return r.turn as TurnState;
}

export async function turnFold(turn: TurnState, event: unknown): Promise<FoldResult> {
  const r = await post<S["FoldResponse"]>("/api/turns/fold", { turn, event: event ?? {} });
  return {
    turn: r.turn as TurnState,
    action: r.action as FoldAction,
    detail: (r.detail as FoldResult["detail"]) ?? null,
  };
}

export async function turnSelectOption(
  turn: TurnState,
  requestId: string,
  qidx: number,
  opt: number,
): Promise<{ turn: TurnState; changed: boolean }> {
  const r = await post<S["SelectOptionResponse"]>("/api/turns/select-option", {
    turn,
    request_id: requestId,
    qidx,
    opt,
  });
  return { turn: r.turn as TurnState, changed: r.changed };
}

export async function turnSetCustom(turn: TurnState, requestId: string, qidx: number): Promise<TurnState> {
  const r = await post<S["TurnPayload"]>("/api/turns/set-custom", {
    turn,
    request_id: requestId,
    qidx,
  });
  return r.turn as TurnState;
}

export async function turnAnswerCustom(
  turn: TurnState,
  requestId: string,
  qidx: number,
  text: string,
): Promise<{ turn: TurnState; complete: boolean; answer: Record<string, unknown> }> {
  const r = await post<S["FormAnswerResponse"]>("/api/turns/answer-custom", {
    turn,
    request_id: requestId,
    qidx,
    text,
  });
  return { turn: r.turn as TurnState, complete: r.complete, answer: (r.answer ?? {}) as Record<string, unknown> };
}

export async function turnSubmitForm(
  turn: TurnState,
  requestId: string,
): Promise<{ turn: TurnState; complete: boolean; answer: Record<string, unknown>; missing: number[] }> {
  const r = await post<S["FormAnswerResponse"]>("/api/turns/submit-form", {
    turn,
    request_id: requestId,
  });
  return {
    turn: r.turn as TurnState,
    complete: r.complete,
    answer: (r.answer ?? {}) as Record<string, unknown>,
    missing: r.missing ?? [],
  };
}

export async function turnDropForm(turn: TurnState, requestId: string): Promise<TurnState> {
  const r = await post<S["TurnPayload"]>("/api/turns/drop-form", { turn, request_id: requestId });
  return r.turn as TurnState;
}

export interface TurnButton {
  text: string;
  data: string;
}

export async function turnRenderRunning(
  turn: TurnState,
  opencodeDir: string,
): Promise<{ text: string; keyboard: TurnButton[][] }> {
  const r = await post<S["RenderRunningResponse"]>("/api/turns/render-running", {
    turn,
    opencode_dir: opencodeDir,
  });
  return { text: r.text, keyboard: (r.keyboard as TurnButton[][]) ?? [] };
}

export async function turnRenderThink(turn: TurnState, elapsed: number, opencodeDir: string): Promise<string> {
  const r = await post<S["TextResponse"]>("/api/turns/render-think", {
    turn,
    elapsed,
    opencode_dir: opencodeDir,
  });
  return r.text;
}

export async function turnRenderResult(turn: TurnState): Promise<string> {
  const r = await post<S["TextResponse"]>("/api/turns/render-result", { turn });
  return r.text;
}

export async function turnRenderDiff(turn: TurnState, path: string, opencodeDir: string): Promise<string> {
  const r = await post<S["TextResponse"]>("/api/turns/render-diff", {
    turn,
    path,
    opencode_dir: opencodeDir,
  });
  return r.text ?? "";
}

export async function turnSplit(text: string, limit = 3500): Promise<string[]> {
  const r = await post<S["SplitResponse"]>("/api/turns/split", { text, limit });
  return r.chunks ?? [text];
}

export async function turnMediaNote(kind: string, filename: string, sizeMb = 0): Promise<string> {
  const r = await post<S["TextResponse"]>("/api/turns/media-note", {
    kind,
    filename,
    size_mb: sizeMb,
  });
  return r.text;
}

export async function turnSafeFilename(name: string, fallback: string): Promise<string> {
  const r = await post<S["SafeFilenameResponse"]>("/api/turns/safe-filename", {
    name,
    default: fallback,
  });
  return r.filename;
}

export async function turnTelegramHtml(text: string, maxLen = 3800): Promise<string> {
  const r = await post<S["HtmlResponse"]>("/api/turns/telegram-html", { text, max_len: maxLen });
  return r.html;
}
