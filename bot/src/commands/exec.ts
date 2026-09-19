/** /exec e /sh — comandos locais via allowlist, sem shell. */
import type { Context } from "grammy";
import { EXEC_HELP, formatExecResult, runExec } from "../services/exec.ts";

export async function execCmd(ctx: Context): Promise<void> {
  const raw = String((ctx.match as string | undefined) ?? "").trim();
  if (!raw) {
    await ctx.reply(`Uso: ${EXEC_HELP}\n\nSem shell: sem pipes/redirecionamentos. /cancel mata o exec.`);
    return;
  }
  const r = await runExec(raw, ctx.chat!.id);
  await ctx.reply(formatExecResult(r), { parse_mode: "HTML" });
}
