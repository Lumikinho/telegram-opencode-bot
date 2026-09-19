/** /funnel [on|off] — Tailscale funnel. */
import type { Context } from "grammy";
import { escHtml } from "../services/restart.ts";
import { funnelOff, funnelOn } from "../services/python.ts";
import { showPanel, screenFunnel } from "../handlers/panels.ts";

export async function funnel(ctx: Context): Promise<void> {
  const arg = ctx.match?.toString().trim().toLowerCase();
  try {
    if (arg === "on" || arg === "off") {
      if (arg === "on") await funnelOn();
      else await funnelOff();
    } else if (arg && arg !== "status") {
      await ctx.reply("Uso: <code>/funnel [on|off]</code>", { parse_mode: "HTML" });
      return;
    }
    const s = await screenFunnel();
    await showPanel(ctx.api, ctx.chat!.id, s.html, s.keyboard);
  } catch (e) {
    await ctx.reply(`[ERR] funnel: ${escHtml(String(e)).slice(0, 200)}`, { parse_mode: "HTML" });
  }
}
