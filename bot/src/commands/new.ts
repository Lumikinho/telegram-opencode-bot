/** /new — nova conversa. */
import type { Context } from "grammy";
import { escHtml } from "../services/restart.ts";
import { createSession } from "../services/opencode.ts";
import { addSession, cfgFor, persistChat, rememberSid } from "../services/store.ts";
import { showPanel, screenOpencode } from "../handlers/panels.ts";

export async function newChat(ctx: Context): Promise<void> {
  const sid = await createSession();
  rememberSid(sid);
  cfgFor(ctx.chat!.id).sid = sid;
  addSession(sid);
  persistChat(ctx.chat!.id);
  const s = screenOpencode(ctx.chat!.id);
  await showPanel(ctx.api, ctx.chat!.id, `[NEW] sessão criada: <code>${escHtml(sid.slice(-6))}</code>\n\n${s.html}`, s.keyboard);
}
