/** /cancel — interrompe turno e execs. */
import type { Context } from "grammy";
import { cfgFor } from "../services/store.ts";
import { interruptSession } from "../services/opencode.ts";
import { cancelExecs } from "../services/exec.ts";
import { cancelTurn } from "../services/turns.ts";
import type { Bot } from "grammy";

export async function cancel(bot: Bot, ctx: Context): Promise<void> {
  const sid = cfgFor(ctx.chat!.id).sid;
  if (sid) await interruptSession(sid);
  const killedExec = await cancelExecs(ctx.chat!.id);
  const had = await cancelTurn(bot, ctx.chat!.id);
  await ctx.reply(killedExec || had ? "[STOP] Interrompido (turno/exec)." : "[STOP] Nada em andamento.");
}
