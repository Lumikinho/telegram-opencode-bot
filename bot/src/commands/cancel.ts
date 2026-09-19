/** /cancel — interrompe turno ativo e execs; não limpa a fila (use /fila). */
import type { Context } from "grammy";
import { cfgFor } from "../services/store.ts";
import { interruptSession } from "../services/opencode.ts";
import { cancelExecs } from "../services/exec.ts";
import { cancelTurn, getQueueSize } from "../services/turns.ts";
import type { Bot } from "grammy";

export async function cancel(bot: Bot, ctx: Context): Promise<void> {
  const chatId = ctx.chat!.id;
  const sid = cfgFor(chatId).sid;
  if (sid) await interruptSession(sid);
  const killedExec = await cancelExecs(chatId);
  const had = await cancelTurn(bot, chatId);
  const queued = getQueueSize(chatId);
  const filaInfo = queued ? ` Fila: ${queued} pedido(s) aguardando (/fila para cancelar).` : "";
  await ctx.reply(
    killedExec || had ? `[STOP] Interrompido (turno/exec).${filaInfo}` : "[STOP] Nada em andamento.",
  );
}
