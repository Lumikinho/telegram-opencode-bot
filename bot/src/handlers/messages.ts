/** Mensagens livres (texto + anexos) -> turno opencode. */
import type { Bot, Context } from "grammy";
import { answerForm, answerPermission } from "../services/opencode.ts";
import { cfgFor, ensureSid } from "../services/store.ts";
import { consumeCustomAnswer, runOrEnqueue } from "../services/turns.ts";
import { captionOf, collectMedia, downloadParts } from "../utils/media.ts";

export function registerMessages(bot: Bot): void {
  bot.on("message:text", async (ctx) => {
    const text = ctx.message.text.trim();
    const chatId = ctx.chat.id;
    if (await consumeCustomAnswer(bot, chatId, text)) return;
    const m = text.match(/^(allow|deny):(\S+)\s*(once|always)?$/i);
    const sid = cfgFor(chatId).sid;
    if (m && sid) {
      const [, verb, id] = m;
      await answerPermission(sid, id, verb.toLowerCase() === "allow" ? "once" : "reject");
      await ctx.reply("[OK] respondido.");
      return;
    }
    const f = text.match(/^form:(\S+)\s+(\{.*\})$/s);
    if (f && sid) {
      try {
        await answerForm(sid, f[1], JSON.parse(f[2]) as Record<string, unknown>);
        await ctx.reply("[OK] form respondido.");
      } catch {
        await ctx.reply("[ERR] form inválido. Use: form:<id> {\"campo\": valor}");
      }
      return;
    }
    const target = await ensureSid(chatId);
    if (!target) {
      await ctx.reply("[ERR] sem sessão (servidor fora?).");
      return;
    }
    const cfg = cfgFor(chatId);
    await runOrEnqueue(bot, chatId, { sid: target, text, model: cfg.model, agent: cfg.agent });
  });

  bot.on(
    [
      "message:photo",
      "message:document",
      "message:audio",
      "message:voice",
      "message:video",
      "message:video_note",
      "message:animation",
    ],
    async (ctx) => {
      const text = await captionOf(ctx as Context);
      if (text && (await consumeCustomAnswer(bot, ctx.chat.id, text))) return;
      const entries = await collectMedia(ctx as Context);
      if (!entries.length && !text) {
        await ctx.reply(
          "[!] Anexos suportados: foto, documento, áudio, voz, vídeo e GIF.\nEnvie junto um texto ou legenda com a instrução.",
        );
        return;
      }
      const target = await ensureSid(ctx.chat.id);
      if (!target) {
        await ctx.reply("[ERR] sem sessão (servidor fora?).");
        return;
      }
      const cfg = cfgFor(ctx.chat.id);
      const parts = entries.length ? await downloadParts(bot, entries) : [];
      const files = parts.filter((p) => p.type === "file");
      const notes = parts.filter((p) => p.type === "text").map((p) => (p as { text: string }).text);
      const prompt = [text, ...notes].filter(Boolean).join("\n");
      await runOrEnqueue(bot, ctx.chat.id, {
        sid: target,
        text: prompt,
        model: cfg.model,
        agent: cfg.agent,
        files: files as { type: "file"; url: string; filename?: string }[],
      });
    },
  );
}
