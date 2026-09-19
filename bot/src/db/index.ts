/** SQLite do gateway (bun:sqlite). Cria o schema no primeiro uso. */
import { Database } from "bun:sqlite";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { DATA_DIR } from "../config/env.ts";
import { SCHEMA } from "./schema.ts";

export interface ChatRow {
  chat_id: number;
  sid: string | null;
  model: string | null;
  agent: string | null;
}

let db: Database | null = null;

export function database(): Database {
  if (!db) {
    const dir = DATA_DIR.startsWith("/") ? DATA_DIR : join(process.cwd(), DATA_DIR);
    mkdirSync(dirname(join(dir, "x")), { recursive: true });
    db = new Database(join(dir, "bot.sqlite"));
    db.run(SCHEMA);
  }
  return db;
}

export function loadChats(): ChatRow[] {
  try {
    return database().query("SELECT chat_id, sid, model, agent FROM chats").all() as ChatRow[];
  } catch {
    return [];
  }
}

export function saveChat(row: ChatRow): void {
  try {
    database().run(
      `INSERT INTO chats (chat_id, sid, model, agent, updated_at)
       VALUES (?, ?, ?, ?, strftime('%s','now'))
       ON CONFLICT(chat_id) DO UPDATE SET
         sid = excluded.sid, model = excluded.model,
         agent = excluded.agent, updated_at = excluded.updated_at`,
      [row.chat_id, row.sid, row.model, row.agent],
    );
  } catch (e) {
    console.warn("db: falha ao salvar chat:", (e as Error)?.message ?? e);
  }
}
