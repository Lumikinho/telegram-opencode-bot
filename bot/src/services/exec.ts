/** Terminal commands via allowlist (TS-first, sem shell).
 *
 * Segurança:
 * - Só o dono chega aqui (middleware `isOwner` em `telegram.ts`).
 * - Sem `bash -c`: argv executado direto via `Bun.spawn`, então
 *   `; | & $() `` ` viram argumentos literais, não chaining.
 * - Pipes/redirecionamentos não suportados (sem shell) — rode um comando por vez.
 * - `cwd` confinado em `OPENCODE_DIR` (`EXEC_CWD` pode apertar, nunca soltar para `/`).
 * - Timeout + teto de saída + cancelamento por chat via `/cancel`.
 */

import { OPENCODE_DIR } from "../config/env";

const DEFAULT_ALLOW = [
  "ls", "pwd", "whoami", "uptime", "df", "free", "du",
  "cat", "head", "tail", "grep", "wc", "ps", "lsblk", "ip", "ping",
  "git", "docker", "systemctl", "journalctl", "tailscale",
  "bun", "node", "python3", "opencode",
  "echo", "printf",
];

function envInt(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const n = parseInt(raw, 10);
  return Number.isFinite(n) ? n : fallback;
}

export function allowList(): Set<string> {
  const raw = process.env.EXEC_ALLOWLIST ?? "";
  const items = raw.split(",").map((s) => s.trim().replace(/\.exe$/i, "")).filter(Boolean);
  const list = items.length ? items : DEFAULT_ALLOW;
  return new Set(list.map((s) => s.split("/").pop() as string));
}

export function execTimeoutMs(): number {
  return envInt("EXEC_TIMEOUT_MS", 30_000);
}

export function execMaxOutput(): number {
  return envInt("EXEC_MAX_OUTPUT", 8000);
}

export function execCwd(): string {
  return process.env.EXEC_CWD || OPENCODE_DIR;
}

/** Split respeitando aspas simples/duplas (sem expansão de shell). */
export function parseArgv(raw: string): string[] {
  const out: string[] = [];
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(raw)) !== null) out.push(m[1] ?? m[2] ?? m[3]);
  return out;
}

export function baseName(argv0: string): string {
  return argv0.split("/").pop() ?? argv0;
}

export function isAllowed(argv0: string, allow = allowList()): boolean {
  return allow.has(baseName(argv0));
}

export interface ExecResult {
  ok: boolean;
  cmd: string;
  argv: string[];
  exitCode: number | null;
  stdout: string;
  stderr: string;
  truncated: boolean;
  durationMs: number;
  error?: string;
}

// chatId -> procs vivos (para /cancel matar exec em andamento)
const running = new Map<number, Set<Bun.Subprocess>>();

function track(chatId: number, proc: Bun.Subprocess): void {
  let s = running.get(chatId);
  if (!s) {
    s = new Set();
    running.set(chatId, s);
  }
  s.add(proc);
}

function untrack(chatId: number, proc: Bun.Subprocess): void {
  running.get(chatId)?.delete(proc);
}

export function runningExecs(chatId: number): number {
  return running.get(chatId)?.size ?? 0;
}

/** Mata execs do chat (true se matou algum). Chamado pelo /cancel. */
export async function cancelExecs(chatId: number): Promise<boolean> {
  const set = running.get(chatId);
  if (!set?.size) return false;
  for (const p of set) {
    try {
      p.kill("SIGTERM");
    } catch { /* já saiu */ }
  }
  await Bun.sleep(500);
  for (const p of set) {
    if (p.exitCode === null) {
      try {
        p.kill("SIGKILL");
      } catch { /* já saiu */ }
    }
  }
  set.clear();
  return true;
}

function cap(s: string, max: number): { text: string; truncated: boolean } {
  if (s.length <= max) return { text: s, truncated: false };
  return { text: s.slice(0, max) + "\n…[truncado]", truncated: true };
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export async function runExec(raw: string, chatId = 0, opts: { timeoutMs?: number; maxOutput?: number; cwd?: string } = {}): Promise<ExecResult> {
  const t0 = Date.now();
  const cmd = raw.trim();
  if (!cmd) return { ok: false, cmd, argv: [], exitCode: null, stdout: "", stderr: "", truncated: false, durationMs: 0, error: "uso: /exec <comando> [args]" };
  const argv = parseArgv(cmd);
  if (!argv.length) return { ok: false, cmd, argv, exitCode: null, stdout: "", stderr: "", truncated: false, durationMs: 0, error: "comando vazio" };
  if (!isAllowed(argv[0])) {
    return {
      ok: false, cmd, argv, exitCode: null, stdout: "", stderr: "",
      truncated: false, durationMs: Date.now() - t0,
      error: `[DENY] comando não permitido: \`${baseName(argv[0])}\`. Permitidos: ${[...allowList()].sort().join(", ")}`,
    };
  }
  const timeoutMs = opts.timeoutMs ?? execTimeoutMs();
  const maxOutput = opts.maxOutput ?? execMaxOutput();
  const cwd = opts.cwd ?? execCwd();
  let proc: Bun.Subprocess;
  try {
    proc = Bun.spawn(argv, { cwd, stdout: "pipe", stderr: "pipe", stdin: "ignore", env: { ...process.env } });
  } catch (e) {
    return { ok: false, cmd, argv, exitCode: null, stdout: "", stderr: "", truncated: false, durationMs: Date.now() - t0, error: `falha ao iniciar: ${e}` };
  }
  if (chatId) track(chatId, proc);
  const timer = setTimeout(() => {
    try {
      proc.kill("SIGTERM");
    } catch { /* já saiu */ }
  }, timeoutMs);
  let timedOut = false;
  const killer = (async () => {
    await proc.exited;
  })();
  const timeoutRace = Bun.sleep(timeoutMs + 1000).then(() => { timedOut = proc.exitCode === null; });
  void timeoutRace;
  const [out, err, code] = await Promise.all([
    new Response(proc.stdout as ReadableStream).text(),
    new Response(proc.stderr as ReadableStream).text(),
    killer.then(() => proc.exitCode),
  ]);
  clearTimeout(timer);
  if (chatId) untrack(chatId, proc);
  if (timedOut) {
    try {
      proc.kill("SIGKILL");
    } catch { /* já saiu */ }
  }
  const durationMs = Date.now() - t0;
  const so = cap(out.replace(/\r\n/g, "\n"), maxOutput);
  const se = cap(err.replace(/\r\n/g, "\n"), maxOutput);
  const truncated = so.truncated || se.truncated || timedOut;
  if (timedOut) {
    return { ok: false, cmd, argv, exitCode: code ?? null, stdout: so.text, stderr: se.text, truncated: true, durationMs, error: `timeout após ${timeoutMs}ms` };
  }
  return { ok: (code ?? 1) === 0, cmd, argv, exitCode: code, stdout: so.text, stderr: se.text, truncated, durationMs };
}

/** Formata resultado para Telegram HTML (<pre>, limite 3500). */
export function formatExecResult(r: ExecResult): string {
  if (r.error && !r.argv.length) return escapeHtml(r.error);
  const head = `<b>$ ${escapeHtml(r.cmd.slice(0, 300))}</b>`;
  if (r.error && r.exitCode === null && !r.stdout && !r.stderr) return `${head}\n${r.error}`;
  const body = [r.stdout, r.stderr ? `\n[stderr]\n${r.stderr}` : ""].join("").trim() || "(sem saída)";
  const foot = `\n———\nexit=${r.exitCode ?? "?"} [T] ${r.durationMs}ms${r.truncated ? " (truncado)" : ""}${r.error ? `\n${escapeHtml(r.error)}` : ""}`;
  const full = `${head}\n<pre>${escapeHtml(body.slice(0, 3200))}</pre>${escapeHtml(foot)}`;
  return full.length > 3800 ? full.slice(0, 3790) + "…</pre>" : full;
}

export const EXEC_HELP = "/exec <cmd> — roda comando permitido (allowlist, sem shell). Ex.: /exec git status";
