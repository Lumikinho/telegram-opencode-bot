/** /sessions — lista sessões do servidor. */
import type { Context } from "grammy";
import { showPanel, screenSessions } from "../handlers/panels.ts";

export async function sessions(ctx: Context): Promise<void> {
  const s = await screenSessions(ctx.chat!.id);
  await showPanel(ctx.api, ctx.chat!.id, s.html, s.keyboard);
}
