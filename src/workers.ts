/** Ponte Bun -> workers Python (`py/*_cli.py`) via JSON stdin/stdout. */
import { BATTERY_PATH } from "./config.ts";

async function runPy(script: string, payload: unknown, timeoutMs = 20_000): Promise<unknown> {
  const proc = Bun.spawn(["python3", script], {
    stdin: "pipe",
    stdout: "pipe",
    stderr: "pipe",
  });
  const body = JSON.stringify(payload ?? {});
  proc.stdin.write(body);
  proc.stdin.end();
  const timer = setTimeout(() => {
    try {
      proc.kill();
    } catch {
      /* timeout */
    }
  }, timeoutMs);
  const [out, err, code] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
    proc.exited,
  ]);
  clearTimeout(timer);
  if (code !== 0) throw new Error(`${script} exit=${code}: ${err || out}`.slice(0, 500));
  const text = out.trim();
  if (!text) throw new Error(`${script}: saída vazia (${err})`.slice(0, 300));
  return JSON.parse(text);
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

export async function batteryOnce(batteryPath = BATTERY_PATH): Promise<BatteryInfo> {
  return (await runPy("py/battery_cli.py", { battery_path: batteryPath })) as BatteryInfo;
}

export interface FunnelEntry {
  url: string;
  on: boolean;
  mappings: string[];
}

export async function funnelStatus(): Promise<{ raw: string; funnels: FunnelEntry[] }> {
  return (await runPy("py/funnel_cli.py", { action: "status" })) as {
    raw: string;
    funnels: FunnelEntry[];
  };
}

export async function funnelOn(): Promise<{ ok: boolean; message: string }> {
  return (await runPy("py/funnel_cli.py", { action: "on" }, 60_000)) as {
    ok: boolean;
    message: string;
  };
}

export async function funnelOff(): Promise<{ ok: boolean; message: string }> {
  return (await runPy("py/funnel_cli.py", { action: "off" }, 30_000)) as {
    ok: boolean;
    message: string;
  };
}

export async function renderMarkdown(text: string): Promise<string> {
  const r = (await runPy("py/render_cli.py", { text })) as { html: string };
  return r.html;
}

// ---- turn worker (py/turn_cli.py) ----

export type TurnState = Record<string, unknown>;

export type FoldAction = "none" | "push" | "push_force" | "finish";

export interface FoldResult {
  turn: TurnState;
  action: FoldAction;
  detail: { reason?: string; error?: string } | null;
}

async function turnCall(payload: Record<string, unknown>, timeoutMs = 20_000): Promise<Record<string, unknown>> {
  const r = (await runPy("py/turn_cli.py", payload, timeoutMs)) as Record<string, unknown>;
  if (r.error) throw new Error(`turn_cli: ${r.error}`);
  return r;
}

export async function turnNew(chatId: number): Promise<TurnState> {
  const r = await turnCall({ action: "new_turn", chat_id: chatId });
  return r.turn as TurnState;
}

export async function turnFold(turn: TurnState, event: unknown): Promise<FoldResult> {
  const r = await turnCall({ action: "fold", turn, event });
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
  const r = await turnCall({ action: "select_option", turn, request_id: requestId, qidx, opt });
  return { turn: r.turn as TurnState, changed: Boolean(r.changed) };
}

export async function turnSetCustom(turn: TurnState, requestId: string, qidx: number): Promise<TurnState> {
  const r = await turnCall({ action: "set_custom", turn, request_id: requestId, qidx });
  return r.turn as TurnState;
}

export async function turnAnswerCustom(
  turn: TurnState,
  requestId: string,
  qidx: number,
  text: string,
): Promise<{ turn: TurnState; complete: boolean; answer: Record<string, unknown> }> {
  const r = await turnCall({ action: "answer_custom", turn, request_id: requestId, qidx, text });
  const res = r.res as { complete: boolean; answer: Record<string, unknown> };
  return { turn: r.turn as TurnState, complete: res.complete, answer: res.answer };
}

export async function turnSubmitForm(
  turn: TurnState,
  requestId: string,
): Promise<{ turn: TurnState; complete: boolean; answer: Record<string, unknown>; missing: number[] }> {
  const r = await turnCall({ action: "submit_form", turn, request_id: requestId });
  return {
    turn: r.turn as TurnState,
    complete: Boolean(r.complete),
    answer: (r.answer as Record<string, unknown>) ?? {},
    missing: (r.missing as number[]) ?? [],
  };
}

export async function turnDropForm(turn: TurnState, requestId: string): Promise<TurnState> {
  const r = await turnCall({ action: "drop_form", turn, request_id: requestId });
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
  const r = await turnCall({ action: "render_running", turn, opencode_dir: opencodeDir });
  return { text: r.text as string, keyboard: (r.keyboard as TurnButton[][]) ?? [] };
}

export async function turnRenderThink(turn: TurnState, elapsed: number, opencodeDir: string): Promise<string> {
  const r = await turnCall({ action: "render_think", turn, elapsed, opencode_dir: opencodeDir });
  return r.text as string;
}

export async function turnRenderResult(turn: TurnState): Promise<string> {
  const r = await turnCall({ action: "render_result", turn });
  return r.text as string;
}

export async function turnSplit(text: string, limit = 3500): Promise<string[]> {
  const r = await turnCall({ action: "split", text, limit });
  return (r.chunks as string[]) ?? [text];
}

export async function turnTelegramHtml(text: string, maxLen = 3800): Promise<string> {
  const r = await turnCall({ action: "telegram_html", text, max_len: maxLen });
  return r.html as string;
}
