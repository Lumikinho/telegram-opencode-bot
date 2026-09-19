/** /agents [nome] — lista ou define o agente. */
import type { Context } from "grammy";
import { cfgFor, persistChat } from "../services/store.ts";
import { showPanel, screenAgents } from "../handlers/panels.ts";

export async function agents(ctx: Context): Promise<void> {
  const arg = ctx.match?.toString().trim();
  if (arg) {
    cfgFor(ctx.chat!.id).agent = arg;
    persistChat(ctx.chat!.id);
  }
  const s = await screenAgents(ctx.chat!.id);
  await showPanel(ctx.api, ctx.chat!.id, s.html, s.keyboard);
}
