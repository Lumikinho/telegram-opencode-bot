/** Logging: cada update + erros sem derrubar o polling. */
import type { Bot, Context } from "grammy";

function describe(ctx: Context): string {
  const kind = ctx.callbackQuery ? `cb:${ctx.callbackQuery.data?.slice(0, 24)}`
    : ctx.message?.text ? `msg:${ctx.message.text.slice(0, 40)}`
    : ctx.message ? "mídia"
    : "?";
  return `chat=${ctx.chat?.id} user=${ctx.from?.id} ${kind}`;
}

export function logging(bot: Bot): void {
  bot.use(async (ctx, next) => {
    const t0 = Date.now();
    try {
      await next();
    } catch (e) {
      console.error(`[ERR] update ${describe(ctx)}:`, e);
      try {
        await ctx.reply("[ERR] falha interna, tente de novo.");
      } catch {
        /* melhor esforço */
      }
    } finally {
      console.log(`[update] ${describe(ctx)} ${Date.now() - t0}ms`);
    }
  });
}
