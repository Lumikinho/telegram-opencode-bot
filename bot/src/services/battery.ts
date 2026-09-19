/** Monitor de bateria (extraído de `telegram.ts`). */
import type { Bot } from "grammy";
import { BATTERY_LOW_PCT, ownerChatId } from "../config/env";
import { batteryOnce } from "./python";

export async function batteryWatch(bot: Bot, stop: () => boolean): Promise<void> {
  await Bun.sleep(10_000);
  let lastStatus: string | null = null;
  let fullNotified = false;
  const { BATTERY_CHECK_INTERVAL } = await import("../config/env");
  for (;;) {
    if (stop()) return;
    try {
      const b = await batteryOnce();
      const pct = b.capacity !== null ? parseInt(b.capacity, 10) : NaN;
      const status = b.status ?? "Unknown";
      const chatId = ownerChatId();
      if (lastStatus !== null && lastStatus !== status) {
        if (status === "Charging") await bot.api.sendMessage(chatId, `[PWR] *Carregador conectado* (${b.capacity}%).`, { parse_mode: "Markdown" });
        else if (status === "Discharging") await bot.api.sendMessage(chatId, `[BAT] *Na bateria* (${b.capacity}%).`, { parse_mode: "Markdown" });
      }
      if (status === "Full" && !fullNotified) {
        fullNotified = true;
        await bot.api.sendMessage(chatId, "[BAT] *Carga completa!*", { parse_mode: "Markdown" });
      } else if (status !== "Full") {
        fullNotified = false;
      }
      if (Number.isFinite(pct) && pct <= BATTERY_LOW_PCT && status !== "Charging" && status !== "Full") {
        await bot.api.sendMessage(chatId, `[!] *Bateria baixa!* ${pct}% restante.`, { parse_mode: "Markdown" });
      }
      lastStatus = status;
    } catch (e) {
      console.warn("battery watch:", e);
    }
    await Bun.sleep(BATTERY_CHECK_INTERVAL * 1000);
  }
}
