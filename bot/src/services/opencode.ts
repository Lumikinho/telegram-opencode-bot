/** Cliente HTTP do opencode server v2 (port de bot/opencode.py).
 *
 * Norma v2: BasicAuth `opencode:<senha>` em toda rota, rotas `/api/*` com
 * envelope `{data}`, SSE em `GET /api/event`, prompt em
 * `POST /api/session/{id}/prompt`, modelo/agente como estado da sessão,
 * permissões `.../permission/{id}/reply`, perguntas como forms.
 */
import { OC_PASSWORD, OC_PORT, OC_URL, OPENCODE_DIR } from "../config/env";
import { splitModelRef } from "../utils/models";

export const endpoint = { port: OC_PORT, url: OC_URL };

/** Cascata de readiness (verificada no fonte do servidor):
 * `/api/health` (V2) → `/global/health` (legado/V1) → `/api/status` (info).
 * Sem o meio-termo, um servidor V1 ativo levava 404 duplo e o bot
 * spawnava um segundo servidor à toa. */
export const HEALTH_PATHS = ["/api/health", "/global/health", "/api/status"] as const;

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
  // Cascata HEALTH_PATHS: 200 vence; 404 tenta a próxima; resto falha fechado.
  for (const path of HEALTH_PATHS) {
    try {
      const r = await api(path, {}, 7_000);
      if (r.status === 200) return true;
      if (r.status !== 404) return false;
    } catch {
      return false;
    }
  }
  return false;
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
  let lastError: string | null = null;
  for (const path of HEALTH_PATHS) {
    try {
      const r = await api(path, {}, 7_000);
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
        return info;
      } else if (r.status === 401) {
        info.error = "HTTP 401: senha do servidor (OPENCODE_SERVER_PASSWORD) incorreta";
        return info;
      } else if (r.status === 404) {
        lastError = `HTTP 404 em ${path}`;
        continue;
      } else {
        info.error = `HTTP ${r.status}`;
        return info;
      }
    } catch (e) {
      info.error = `${e}`.slice(0, 200);
      return info;
    }
  }
  info.error = lastError;
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
    // Explícito: cauda newest-first (o default do servidor já é esse, mas
    // depender de default silencioso quebra sem aviso).
    const r = await api("/api/session?limit=50&order=desc", {}, 30_000);
    if (!r.ok) return [];
    const data = ((await r.json()) as { data?: unknown })?.data;
    return Array.isArray(data) ? (data as SessionSummary[]) : [];
  } catch (e) {
    console.warn("Falha ao listar sessões:", e);
    return [];
  }
}

/** Escolha de sessão sem rede (pura, testável — port de pick_session_id).
 *
 * 1) sid guardado ainda existe → mantém;
 * 2) senão, a mais recente APENAS entre as conhecidas (known);
 * 3) sem candidatas → null (quem chama cria uma nova).
 *
 * O filtro `known` existe porque readotar "a mais recente de todas" pode
 * anexar o chat a uma sessão viva de outro cliente (TUI/CLI) — e o bot
 * passa a espelhar atividade alheia no Telegram. `known` omitido = legado
 * (qualquer uma), para compatibilidade com chamadores antigos. */
export function pickSessionId(
  storedSid: string | null | undefined,
  sessions: SessionSummary[],
  known?: Set<string> | null,
): string | null {
  const ids = new Set(sessions.map((s) => s.id).filter(Boolean));
  if (storedSid && ids.has(storedSid)) return storedSid;
  if (!sessions.length) return null;
  const pool = known === undefined || known === null
    ? sessions
    : sessions.filter((s) => s.id && known.has(s.id));
  if (!pool.length) return null;
  const updated = (s: SessionSummary): number => s.time?.updated ?? 0;
  const ordered = [...pool].sort((a, b) => updated(b) - updated(a));
  return ordered[0]?.id ?? null;
}

export async function restoreChatSession(
  chatCfg: { sid?: string | null },
  sessions: SessionSummary[],
  known?: Set<string> | null,
): Promise<{ sid: string | null; created: boolean }> {
  const sid = pickSessionId(chatCfg.sid, sessions, known);
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
    const ref = splitModelRef(opts.model);
    const r = await api(`/api/session/${sid}/model`, {
      method: "POST",
      body: JSON.stringify({
        model: ref.variant
          ? { providerID: ref.providerID, id: ref.modelID, variant: ref.variant }
          : { providerID: ref.providerID, id: ref.modelID },
      }),
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

interface AssistantMsg {
  type?: string;
  content?: { type?: string; text?: string }[];
}

/** Primeiro texto de assistant numa página **desc** (mais nova primeiro). Puro, testável. */
export function latestAssistantText(messages: unknown, limit = 3500): string {
  if (!Array.isArray(messages)) return "";
  for (const m of messages as AssistantMsg[]) {
    if (!m || typeof m !== "object" || m.type !== "assistant") continue;
    const texts = (m.content ?? [])
      .filter((p) => p?.type === "text" && (p.text ?? "").trim())
      .map((p) => p.text as string);
    if (texts.length) return texts.join("\n").trim().slice(0, limit);
  }
  return "";
}

/** Último texto do assistente persistido: lê só a cauda, não a conversa inteira. */
export async function lastAssistantText(sid: string, limit = 3500): Promise<string> {
  try {
    const r = await api(`/api/session/${sid}/message?limit=50&order=desc`, {}, 30_000);
    if (!r.ok) return "";
    const data = ((await r.json()) as { data?: unknown })?.data;
    return latestAssistantText(data, limit);
  } catch (e) {
    console.warn("Falha ao ler mensagens da sessão:", e);
    return "";
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

export interface ModelEntry {
  providerID: string;
  modelID: string;
  variants: string[];
}

/** Fallback p/ servidores antigos sem `cost`: lista conhecida custo 0/0.
 * O filtro principal é automático via `cost` do /api/model (ver isFreeModel). */
export const AVAILABLE_MODELS: ReadonlySet<string> = new Set([
  "opencode/muse-spark-1.3-contributor-free",
  "opencode/ling-3.0-flash-fin-free",
  "opencode/nemotron-3.5-lightning-free",
  "opencode/mimo-v2.5-free",
  "opencode/big-pickle",
  "google/gemma-4-26b-a4b-it",
  "google/veo-3.1-lite-generate-preview",
  "google/lyria-3-clip-preview",
]);

type RawModel = {
  enabled?: boolean;
  providerID?: string;
  modelID?: string;
  variants?: { id?: string }[];
  cost?: { input?: number; output?: number }[];
};

/** True se usável sem pagamento: todo tier com input==0 e output==0. Automático. */
export function isFreeModel(m: RawModel): boolean {
  if (!Array.isArray(m.cost) || !m.cost.length) {
    return !!m.providerID && !!m.modelID && AVAILABLE_MODELS.has(`${m.providerID}/${m.modelID}`);
  }
  return m.cost.every((t) => (t?.input ?? 0) === 0 && (t?.output ?? 0) === 0);
}

let modelCache: { at: number; entries: ModelEntry[] } | null = null;
const MODEL_CACHE_TTL = 60_000;

async function fetchModelEntries(): Promise<ModelEntry[]> {
  const r = await api("/api/model", {}, 30_000);
  if (!r.ok) return [];
  const data = ((await r.json()) as { data?: unknown })?.data ?? [];
  return (
    (data as RawModel[])
      .filter((m) => m.enabled !== false && m.providerID && m.modelID && isFreeModel(m))
      .map((m) => ({
        providerID: m.providerID as string,
        modelID: m.modelID as string,
        variants: (m.variants ?? []).map((v) => v.id).filter((v): v is string => !!v),
      }))
  );
}

export async function modelCatalog(): Promise<ModelEntry[]> {
  if (modelCache && Date.now() - modelCache.at < MODEL_CACHE_TTL) return modelCache.entries;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const entries = await fetchModelEntries();
      if (entries.length || attempt === 3) {
        modelCache = { at: Date.now(), entries };
        return entries;
      }
    } catch {
      return modelCache?.entries ?? [];
    }
    await Bun.sleep(3000);
  }
  return modelCache?.entries ?? [];
}

export async function listModels(): Promise<string[]> {
  return (await modelCatalog()).map((m) => `${m.providerID}/${m.modelID}`);
}

/** Ids de variante ("low", "medium", "high"...) de um "provedor/modelo". */
export async function modelVariants(spec: string): Promise<string[]> {
  const base = (spec ?? "").split("#")[0];
  const hit = (await modelCatalog()).find((m) => `${m.providerID}/${m.modelID}` === base);
  return hit?.variants ?? [];
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
