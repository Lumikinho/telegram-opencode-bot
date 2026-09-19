/** Rate limit simples em memória: 30 updates/min por chat.
 *
 * O bot é só do dono; o limite existe contra loops (ex.: botões
 * clicados em rajada ou eco de outro bot no mesmo chat).
 */
import type { Context, NextFunction } from "grammy";

const WINDOW_MS = 60_000;
const MAX_HITS = 30;
const hits = new Map<number, number[]>();

export function rateLimit(ctx: Context, next: NextFunction): Promise<void> {
  const chatId = ctx.chat?.id;
  if (!chatId) return next();
  const now = Date.now();
  const arr = (hits.get(chatId) ?? []).filter((t) => now - t < WINDOW_MS);
  arr.push(now);
  hits.set(chatId, arr);
  if (arr.length > MAX_HITS) {
    return ctx.reply("[STOP] Calma — muitas mensagens em sequência, aguarde um minuto.")
      .then(() => undefined);
  }
  return next();
}
