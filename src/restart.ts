/** Ciclo de vida: servidor opencode + processo do bot (/restart).
 * Port de `bot/restart.py`: alvos bot|server|both, kill por padrão de
 * processo com teto de rodadas, relançamento via run-hybrid.sh.
 */
import { type Bot } from "grammy";
import { endpoint, ensureServer, refreshEndpointFromEnv, stopServer } from "./opencode.ts";
import { killAllTurns } from "./turns.ts";

export type RestartTarget = "bot" | "server" | "both";

const ALIASES: Record<string, RestartTarget> = {
  bot: "bot",
  server: "server",
  servidor: "server",
  srv: "server",
  both: "both",
  ambos: "both",
  tudo: "both",
  all: "both",
};

export const RESTART_LABELS: Record<RestartTarget, string> = {
  bot: "o bot",
  server: "o servidor",
  both: "o bot + o servidor",
};

export function parseRestartTarget(arg: string | undefined): RestartTarget | null {
  if (!arg) return "both";
  return ALIASES[arg.trim().toLowerCase()] ?? null;
}

function serverPattern(port: number): string {
  return `opencode serve --port ${port}`;
}

function pidsOf(port: number): number[] {
  const args = ["pgrep", "-f", serverPattern(port)];
  try {
    const uid = typeof process.getuid === "function" ? process.getuid() : undefined;
    if (uid !== undefined) args.splice(1, 0, "-u", String(uid));
  } catch {
    /* sem getuid: pgrep sem filtro de usuário */
  }
  try {
    const out = Bun.spawnSync(args);
    return out.stdout
      .toString()
      .split(/\s+/)
      .map((x) => parseInt(x, 10))
      .filter((n) => Number.isFinite(n) && n !== process.pid);
  } catch {
    return [];
  }
}

function alive(pids: number[]): number[] {
  return pids.filter((pid) => {
    try {
      process.kill(pid, 0);
      return true;
    } catch (e) {
      const code = (e as NodeJS.ErrnoException)?.code;
      if (code === "EPERM") return true; // existe, sem permissão: trata como vivo
      return false;
    }
  });
}

/** Derruba `opencode serve --port` (com SIGTERM, depois SIGKILL, teto de
 * 3 rodadas). Retorna true se não sobrou nenhum. */
export async function killServers(ports?: Set<number>, timeoutMs = 15_000, maxRounds = 3): Promise<boolean> {
  const targets = ports ?? new Set([endpoint.port]);
  for (let round = 1; round <= maxRounds; round++) {
    let pids: number[] = [];
    for (let i = 0; i < 3; i++) {
      for (const p of targets) pids.push(...pidsOf(p));
      pids = [...new Set(pids)];
      if (pids.length) break;
      await Bun.sleep(300);
    }
    if (!pids.length) return true;
    for (const pid of pids) {
      try {
        process.kill(pid, "SIGTERM");
      } catch {
        /* já saiu */
      }
    }
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (!alive(pids).length) return true;
      await Bun.sleep(250);
    }
    for (const pid of pids) {
      try {
        process.kill(pid, "SIGKILL");
      } catch {
        /* já saiu */
      }
    }
    await Bun.sleep(500);
    const still = alive(pids);
    if (!still.length) return true;
    if (round === maxRounds) console.error(`restart: pids teimosos nas portas ${[...targets]}: ${still}`);
  }
  return false;
}

function respawnBotProcess(): void {
  const root = `${import.meta.dir}/..`;
  const proc = Bun.spawn(["bash", "run-hybrid.sh"], {
    cwd: root,
    stdin: "ignore",
    stdout: Bun.file(`${root}/bot.log`),
    stderr: Bun.file(`${root}/bot.log`),
    env: { ...process.env },
  });
  proc.unref();
  console.log(`restart: bot relançado (pid=${proc.pid})`);
}

async function respawnAndExit(bot: Bot): Promise<void> {
  respawnBotProcess();
  await Bun.sleep(1000);
  try {
    await bot.stop();
  } catch {
    /* polling já parado */
  }
  process.exit(0);
}

/** Recicla o `opencode serve` mantendo o processo do bot (port de _cycle_server_locked). */
export async function cycleServer(): Promise<void> {
  await refreshEndpointFromEnv();
  await stopServer();
  await killServers(new Set([endpoint.port]));
  await ensureServer();
  console.log(`restart: servidor reciclado (porta=${endpoint.port})`);
}

async function restartServerOnly(bot: Bot, chatId: number): Promise<void> {
  try {
    await killAllTurns(bot);
    await cycleServer();
    await bot.api.sendMessage(chatId, `✅ *Servidor reiniciado* — servidor \`${endpoint.url}\` OK. O bot continuou no ar.`, {
      parse_mode: "Markdown",
    }).catch(() => {});
  } catch (e) {
    console.error("restart do servidor falhou:", e);
    await bot.api.sendMessage(
      chatId,
      `❌ *Falha no restart do servidor:* \`${e}\`\n\nVerifique o log: \`.opencode_bot_server.log\``,
      { parse_mode: "Markdown" },
    ).catch(() => {});
  }
}

async function restartBotOnly(bot: Bot, chatId: number): Promise<void> {
  await bot.api.sendMessage(chatId, "🔁 *Reiniciando o bot...* volto em segundos. O servidor continua no ar.", {
    parse_mode: "Markdown",
  }).catch(() => {});
  await killAllTurns(bot);
  keepServerAliveOnExit();
  await respawnAndExit(bot);
}

async function restartBoth(bot: Bot, chatId: number): Promise<void> {
  await bot.api.sendMessage(chatId, "🔁 *Reiniciando bot + servidor...* volto em segundos.", {
    parse_mode: "Markdown",
  }).catch(() => {});
  await killAllTurns(bot);
  try {
    await killServers(new Set([endpoint.port]));
  } catch (e) {
    console.warn("restart duplo: falha ao derrubar servidor:", e);
  }
  await stopServer();
  keepServerAliveOnExit();
  await respawnAndExit(bot);
}

// O shutdown do index.ts mata o servidor que subimos; no restart do bot o
// servidor deve continuar no ar para o processo novo. Como o exit passa por
// outro caminho (process.exit direto), basta não chamar stopServer — este
// hook documenta a intenção e desarma o flag caso o shutdown rode no meio.
let exitKeepsServer = false;

export function keepServerAliveOnExit(): void {
  exitKeepsServer = true;
}

export function shouldKeepServerOnExit(): boolean {
  return exitKeepsServer;
}

/** Despacha o /restart: `bot`, `server` ou `both`. */
export async function performRestart(bot: Bot, chatId: number, target: RestartTarget): Promise<void> {
  if (target === "bot") await restartBotOnly(bot, chatId);
  else if (target === "server") await restartServerOnly(bot, chatId);
  else await restartBoth(bot, chatId);
}

export function restartConfirmKeyboard(): { text: string; data: string }[][] {
  return [
    [
      { text: "🤖 Só o bot", data: "__restart:bot" },
      { text: "🖥️ Só o servidor", data: "__restart:server" },
    ],
    [{ text: "🔁 Bot + servidor", data: "__restart:both" }],
    [{ text: "❌ Cancelar", data: "/status" }],
  ];
}
