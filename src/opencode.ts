/** Cliente HTTP do opencode server v2 (port de bot/opencode.py).
 *
 * Norma v2: BasicAuth `opencode:<senha>` em toda rota, rotas `/api/*` com
 * envelope `{data}`, SSE em `GET /api/event`, prompt em
 * `POST /api/session/{id}/prompt`, modelo/agente como estado da sessão,
 * permissões `.../permission/{id}/reply`, perguntas como forms.
 */
import { OC_PASSWORD, OC_PORT, OC_URL, OPENCODE_DIR } from "./config.ts";

export const endpoint = { port: OC_PORT, url: OC_URL };

/** Relê porta/URL do `.env` em disco (sem tocar no process.env), para que
 * o /restart respeite uma mudança de porta feita no arquivo. */
export async function refreshEndpointFromEnv(): Promise<{ port: number; url: string }> {
  try {
    const t = await Bun.file(`${import.meta.dir}/../.env`).text();
    const vals: Record<string, string> = {};
    for (const line of t.split(/\r?\n/)) {
      const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
      if (m) vals[m[1]] = m[2].replace(/^["']|["']$/g, "");
    }
    const raw = (vals.OPENCODE_SERVER_PORT ?? "").trim();
    if (/^\d+$/.test(raw)) endpoint.port = parseInt(raw, 10);
    endpoint.url = (vals.OPENCODE_SERVER_URL ?? "").trim() || `http://127.0.0.1:${endpoint.port}`;
  } catch {
    /* sem .env legível: mantém o vigente */
  }
  return { ...endpoint };
}

export let serverPassword = OC_PASSWORD;

export function authHeader(pw = serverPassword): Record<string, string> {
  if (!pw) return {};
  const raw = Buffer.from(`opencode:${pw}`).toString("base64");
  return { Authorization: `Basic ${raw}` };
}

async function api(
  path: string,
  init: RequestInit = {},
  timeoutMs = 30_000,
): Promise<Response> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(`${endpoint.url}${path}`, {
      ...init,
      signal: ctrl.signal,
      headers: {
        "Content-Type": "application/json",
        ...authHeader(),
        ...(init.headers as Record<string, string> | undefined),
      },
    });
  } finally {
    clearTimeout(t);
  }
}

export async function serverOk(): Promise<boolean> {
  try {
    const r = await api("/api/status", {}, 7_000);
    return r.status === 200;
  } catch {
    return false;
  }
}

export interface ServerInfo {
  ok: boolean;
  status: "ativo" | "desativado";
  latencyMs: number | null;
  url: string;
  port: number;
  version: string | null;
  error: string | null;
}

export async function serverInfo(): Promise<ServerInfo> {
  const info: ServerInfo = {
    ok: false,
    status: "desativado",
    latencyMs: null,
    url: endpoint.url,
    port: endpoint.port,
    version: null,
    error: null,
  };
  const t0 = Date.now();
  try {
    const r = await api("/api/status", {}, 7_000);
    info.latencyMs = Date.now() - t0;
    if (r.status === 200) {
      info.ok = true;
      info.status = "ativo";
      try {
        const body = (await r.json()) as { version?: string | null };
        info.version = body?.version ?? null;
      } catch {
        /* sem versão */
      }
    } else if (r.status === 401) {
      info.error = "HTTP 401: senha do servidor (OPENCODE_SERVER_PASSWORD) incorreta";
    } else {
      info.error = `HTTP ${r.status}`;
    }
  } catch (e) {
    info.error = `${e}`.slice(0, 200);
  }
  return info;
}

let serverProc: Bun.Subprocess | null = null;
let weStartedServer = false;

function killStaleServers(): number[] {
  // Derruba `opencode serve --port <porta>` órfãos de boot anterior.
  try {
    const out = Bun.spawnSync(["pgrep", "-f", `opencode serve --port ${endpoint.port}`]);
    const txt = out.stdout.toString();
    const killed: number[] = [];
    for (const line of txt.split(/\r?\n/)) {
      const pid = parseInt(line.trim(), 10);
      if (!Number.isFinite(pid) || pid === process.pid) continue;
      try {
        process.kill(pid, "SIGTERM");
        killed.push(pid);
      } catch {
        /* já saiu */
      }
    }
    return killed;
  } catch {
    return [];
  }
}

export async function startServer(): Promise<void> {
  const stale = killStaleServers();
  if (stale.length) console.log(`Servidores órfãos encerrados: ${stale}`);
  if (stale.length) await Bun.sleep(1000);
  if (!serverPassword) {
    serverPassword = Buffer.from(crypto.getRandomValues(new Uint8Array(18))).toString("base64url");
  }
  const logPath = `${OPENCODE_DIR}/.opencode_bot_server.log`;
  const proc = Bun.spawn(["opencode", "serve", "--port", String(endpoint.port), "--print-logs"], {
    cwd: OPENCODE_DIR,
    stdout: Bun.file(logPath),
    stderr: Bun.file(logPath),
    env: { ...process.env, OPENCODE_PASSWORD: serverPassword },
  });
  serverProc = proc;
  weStartedServer = true;
  for (let i = 0; i < 200; i++) {
    if (await serverOk()) {
      console.log(`opencode server v2 pronto na porta ${endpoint.port}`);
      return;
    }
    if (proc.exitCode !== null) throw new Error(`opencode serve encerrou sozinho (code=${proc.exitCode})`);
    await Bun.sleep(500);
  }
  throw new Error("opencode server não respondeu a tempo");
}

export async function ensureServer(): Promise<void> {
  try {
    if (await serverOk()) {
      console.log(`Conectado ao opencode server ${endpoint.url}`);
      return;
    }
  } catch {
    /* cai para o spawn */
  }
  await startServer();
}

export async function stopServer(): Promise<void> {
  if (serverProc && weStartedServer && serverProc.exitCode === null) {
    try {
      serverProc.kill();
    } catch {
      /* ignora */
    }
  }
  serverProc = null;
}

export interface SessionSummary {
  id: string;
  time?: { updated?: number };
}

export async function createSession(): Promise<string> {
  const r = await api(
    "/api/session",
    { method: "POST", body: JSON.stringify({ directory: OPENCODE_DIR }) },
    30_000,
  );
  if (!r.ok) throw new Error(`create session HTTP ${r.status}`);
  const j = (await r.json()) as { data: { id: string } };
  return j.data.id;
}

export async function listSessions(): Promise<SessionSummary[]> {
  try {
    const r = await api("/api/session", {}, 30_000);
    if (!r.ok) return [];
    const data = ((await r.json()) as { data?: unknown })?.data;
    return Array.isArray(data) ? (data as SessionSummary[]) : [];
  } catch (e) {
    console.warn("Falha ao listar sessões:", e);
    return [];
  }
}

/** Escolha de sessão sem rede (pura, testável — port de pick_session_id). */
export function pickSessionId(
  storedSid: string | null | undefined,
  sessions: SessionSummary[],
): string | null {
  const ids = new Set(sessions.map((s) => s.id).filter(Boolean));
  if (storedSid && ids.has(storedSid)) return storedSid;
  if (!sessions.length) return null;
  const updated = (s: SessionSummary): number => s.time?.updated ?? 0;
  const ordered = [...sessions].sort((a, b) => updated(b) - updated(a));
  return ordered[0]?.id ?? null;
}

export async function restoreChatSession(
  chatCfg: { sid?: string | null },
  sessions: SessionSummary[],
): Promise<{ sid: string | null; created: boolean }> {
  const sid = pickSessionId(chatCfg.sid, sessions);
  if (sid) {
    chatCfg.sid = sid;
    return { sid, created: false };
  }
  try {
    const fresh = await createSession();
    chatCfg.sid = fresh;
    sessions.push({ id: fresh, time: { updated: 0 } });
    return { sid: fresh, created: true };
  } catch (e) {
    console.warn("Falha ao criar sessão:", e);
    return { sid: null, created: false };
  }
}

export interface FilePart {
  type: "file";
  url: string;
  filename?: string;
}

export async function sendMessage(
  sid: string,
  text: string,
  opts: { model?: string; agent?: string; files?: FilePart[] } = {},
): Promise<void> {
  if (opts.model) {
    const [providerID, ...rest] = opts.model.split("/");
    const r = await api(`/api/session/${sid}/model`, {
      method: "POST",
      body: JSON.stringify({ model: { providerID, id: rest.join("/") } }),
    });
    if (!r.ok) throw new Error(`model HTTP ${r.status}`);
  }
  if (opts.agent) {
    const r = await api(`/api/session/${sid}/agent`, {
      method: "POST",
      body: JSON.stringify({ agent: opts.agent }),
    });
    if (!r.ok) throw new Error(`agent HTTP ${r.status}`);
  }
  const files = (opts.files ?? []).map((f) => ({
    uri: f.url,
    ...(f.filename ? { name: f.filename } : {}),
  }));
  const r = await api(
    `/api/session/${sid}/prompt`,
    {
      method: "POST",
      body: JSON.stringify({ text, ...(files.length ? { files } : {}) }),
    },
    600_000,
  );
  if (!r.ok) throw new Error(`prompt HTTP ${r.status}`);
}

export async function answerPermission(sid: string, permId: string, reply: string): Promise<void> {
  await api(`/api/session/${sid}/permission/${permId}/reply`, {
    method: "POST",
    body: JSON.stringify({ reply }),
  });
}

export async function answerForm(sid: string, formId: string, answer: Record<string, unknown>): Promise<boolean> {
  try {
    const r = await api(`/api/session/${sid}/form/${formId}/reply`, {
      method: "POST",
      body: JSON.stringify({ answer }),
    });
    return r.status < 400;
  } catch {
    return false;
  }
}

export async function rejectForm(sid: string, formId: string): Promise<boolean> {
  try {
    const r = await api(`/api/session/${sid}/form/${formId}/cancel`, { method: "POST" });
    return r.status < 400;
  } catch {
    return false;
  }
}

export async function interruptSession(sid: string): Promise<void> {  if (!sid) return;
  try {
    await api(`/api/session/${sid}/interrupt`, { method: "POST" }, 10_000);
  } catch {
    /* melhor esforço */
  }
}

export async function listAgents(): Promise<string[]> {
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const r = await api("/api/agent", {}, 30_000);
      if (!r.ok) return [];
      const data = ((await r.json()) as { data?: unknown })?.data ?? [];
      const out = (data as { name?: string; id?: string; hidden?: boolean }[])
        .filter((a) => (a.name ?? a.id) && !a.hidden)
        .map((a) => (a.name ?? a.id) as string);
      if (out.length || attempt === 3) return out;
    } catch {
      return [];
    }
    await Bun.sleep(3000);
  }
  return [];
}

export async function listModels(): Promise<string[]> {
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const r = await api("/api/model", {}, 30_000);
      if (!r.ok) return [];
      const data = ((await r.json()) as { data?: unknown })?.data ?? [];
      const out = (data as { enabled?: boolean; providerID?: string; modelID?: string }[])
        .filter((m) => m.enabled !== false && m.providerID && m.modelID)
        .map((m) => `${m.providerID}/${m.modelID}`);
      if (out.length || attempt === 3) return out;
    } catch {
      return [];
    }
    await Bun.sleep(3000);
  }
  return [];
}

export type ServerEvent = { type: string; data?: Record<string, unknown> };

/** Consome `GET /api/event` (SSE) chamando onEvent; reconecta com backoff. */
export async function consumeEvents(
  onEvent: (ev: ServerEvent) => void,
  shouldStop: () => boolean,
): Promise<void> {
  while (!shouldStop()) {
    try {
      const res = await fetch(`${endpoint.url}/api/event`, { headers: authHeader() });
      if (!res.ok || !res.body) throw new Error(`event HTTP ${res.status}`);
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        if (shouldStop()) {
          reader.cancel();
          return;
        }
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() ?? "";
        for (const line of lines) {
          const t = line.trim();
          if (!t.startsWith("data:")) continue;
          try {
            onEvent(JSON.parse(t.slice(5).trim()) as ServerEvent);
          } catch {
            /* frame parcial */
          }
        }
      }
    } catch (e) {
      console.warn("SSE caiu, reconectando:", e);
    }
    await Bun.sleep(3000);
  }
}
