/** Auth: só o dono fala com o bot. */
import type { Context, NextFunction } from "grammy";
import { OWNER_ID } from "../config/env.ts";

export function isOwner(id?: number): boolean {
  return id === OWNER_ID;
}

export async function ownerOnly(ctx: Context, next: NextFunction): Promise<void> {
  if (!isOwner(ctx.from?.id)) {
    await ctx.reply("[DENY] Acesso negado.");
    return;
  }
  await next();
}
