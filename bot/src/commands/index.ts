/** Registro de comandos: um arquivo por comando, todos ligados aqui. */
import type { Bot } from "grammy";
import type { RestartHooks } from "../services/restart.ts";
import { start } from "./start.ts";
import { help } from "./help.ts";
import { menu } from "./menu.ts";
import { newChat } from "./new.ts";
import { cancel } from "./cancel.ts";
import { fila } from "./fila.ts";
import { status } from "./status.ts";
import { bateria } from "./bateria.ts";
import { funnel } from "./funnel.ts";
import { models } from "./models.ts";
import { agents } from "./agents.ts";
import { sessions } from "./sessions.ts";
import { restartCommand } from "./restart.ts";
import { execCmd } from "./exec.ts";

export const COMMANDS = [
  ["start", "painel principal"],
  ["menu", "painel principal"],
  ["help", "ajuda"],
  ["new", "nova conversa"],
  ["cancel", "interrompe turno/exec"],
  ["fila", "ver/cancelar pedidos na fila"],
  ["status", "estado do servidor"],
  ["bateria", "nível da bateria"],
  ["funnel", "funnel Tailscale [on|off]"],
  ["models", "lista/define modelo"],
  ["agents", "lista/define agente"],
  ["sessions", "lista sessões"],
  ["exec", "comando local permitido"],
  ["restart", "reinicia bot/servidor"],
] as const;

export function registerCommands(bot: Bot, hooks: RestartHooks): void {
  bot.command("start", start);
  bot.command("help", help);
  bot.command("menu", menu);
  bot.command("new", newChat);
  bot.command("cancel", (ctx) => cancel(bot, ctx));
  bot.command("fila", (ctx) => fila(bot, ctx));
  bot.command("queue", (ctx) => fila(bot, ctx));
  bot.command("status", status);
  bot.command("bateria", bateria);
  bot.command("funnel", funnel);
  bot.command("models", models);
  bot.command("agents", agents);
  bot.command("sessions", sessions);
  bot.command("exec", execCmd);
  bot.command("sh", execCmd);
  bot.command("restart", restartCommand(bot, hooks));
}
