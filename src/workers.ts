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
