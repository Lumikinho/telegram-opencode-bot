/** /models [provedor/nome[#variante]] — lista ou define o modelo. */
import type { Context } from "grammy";
import { escHtml } from "../services/restart.ts";
import { modelVariants } from "../services/opencode.ts";
import { baseModelSpec, formatModelRef, splitModelRef } from "../utils/models.ts";
import { cfgFor, persistChat } from "../services/store.ts";
import { showPanel, screenModels } from "../handlers/panels.ts";

export async function models(ctx: Context): Promise<void> {
  const arg = ctx.match?.toString().trim();
  if (arg) {
    const ref = splitModelRef(arg);
    if (!ref.providerID || !ref.modelID) {
      await ctx.reply("Formato: <code>/models provedor/nome[#variante]</code>", { parse_mode: "HTML" });
      return;
    }
    if (ref.variant) {
      const valid = await modelVariants(baseModelSpec(arg));
      if (!valid.includes(ref.variant)) {
        await ctx.reply(
          `[ERR] variante <code>${escHtml(ref.variant)}</code> inválida para <code>${escHtml(baseModelSpec(arg))}</code>` +
            (valid.length ? `\nVálidas: <code>${valid.map(escHtml).join("</code>, <code>")}</code>` : ""),
          { parse_mode: "HTML" },
        );
        return;
      }
    }
    cfgFor(ctx.chat!.id).model = formatModelRef(ref);
    persistChat(ctx.chat!.id);
  }
  const s = await screenModels(ctx.chat!.id, cfgFor(ctx.chat!.id).modelsPage ?? 0);
  await showPanel(ctx.api, ctx.chat!.id, s.html, s.keyboard);
}
