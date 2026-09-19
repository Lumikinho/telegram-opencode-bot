/** /fila — mostra ativo + pedidos enfileirados com botões de cancelar. */
import type { Bot, Context } from "grammy";
import { formatQueueHtml, getQueue, queueListKeyboard } from "../services/turns.ts";

export async function fila(bot: Bot, ctx: Context): Promise<void> {
  const chatId = ctx.chat!.id;
  void bot;
  const items = getQueue(chatId);
  const html = formatQueueHtml(chatId);
  await ctx.reply(html, {
    parse_mode: "HTML",
    reply_markup: queueListKeyboard(items),
  });
}
