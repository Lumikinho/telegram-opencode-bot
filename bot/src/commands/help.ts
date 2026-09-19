/** /help — ajuda. */
import type { Context } from "grammy";
import { showPanel, screenHelp } from "../handlers/panels.ts";

export async function help(ctx: Context): Promise<void> {
  const s = screenHelp();
  await showPanel(ctx.api, ctx.chat!.id, s.html, s.keyboard);
}
