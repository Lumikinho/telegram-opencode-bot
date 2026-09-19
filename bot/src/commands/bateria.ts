/** /bateria — nível da bateria. */
import type { Context } from "grammy";
import { showPanel, screenBattery } from "../handlers/panels.ts";

export async function bateria(ctx: Context): Promise<void> {
  const s = await screenBattery();
  await showPanel(ctx.api, ctx.chat!.id, s.html, s.keyboard);
}
