/** /restart [bot|servidor|ambos] — reinicia. */
import type { Bot, Context } from "grammy";
import { parseRestartTarget, performRestart, type RestartHooks } from "../services/restart.ts";
import { showPanel, screenRestart } from "../handlers/panels.ts";

export function restartCommand(bot: Bot, hooks: RestartHooks) {
  return async (ctx: Context): Promise<void> => {
    const arg = ctx.match?.toString().trim();
    if (arg) {
      const target = parseRestartTarget(arg);
      if (!target) {
        await ctx.reply("Uso: <code>/restart</code> ou <code>/restart bot|servidor|ambos</code>", { parse_mode: "HTML" });
        return;
      }
      await performRestart(bot, ctx.chat!.id, target, hooks);
      return;
    }
    const s = screenRestart();
    await showPanel(ctx.api, ctx.chat!.id, s.html, s.keyboard);
  };
}
