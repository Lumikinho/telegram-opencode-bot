/** /menu — painel principal (alias do /start). */
import type { Context } from "grammy";
import { showPanel, screenMain } from "../handlers/panels.ts";

export async function menu(ctx: Context): Promise<void> {
  const s = await screenMain(ctx.chat!.id);
  await showPanel(ctx.api, ctx.chat!.id, s.html, s.keyboard);
}
