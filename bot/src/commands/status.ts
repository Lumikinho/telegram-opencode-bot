/** /status — estado do servidor. */
import type { Context } from "grammy";
import { showPanel, screenServer } from "../handlers/panels.ts";

export async function status(ctx: Context): Promise<void> {
  const s = await screenServer();
  await showPanel(ctx.api, ctx.chat!.id, s.html, s.keyboard);
}
