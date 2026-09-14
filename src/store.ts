/** Persistência local com `bun:sqlite`: chats (sid/model/agent) sobrevivem
 * a reinícios do gateway. Substitui o PicklePersistence do núcleo Python.
 */
import { Database } from "bun:sqlite";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { OPENCODE_DIR } from "./config.ts";

export interface ChatRow {
  chat_id: number;
  sid: string | null;
  model: string | null;
  agent: string | null;
}

let db: Database | null = null;
let dbPath = "";

export function dbFile(): string {
  return process.env.HYBRID_DB ?? `${OPENCODE_DIR}/.opencode_bot_hybrid.sqlite`;
}

export function openStore(path = dbFile()): Database {
  if (db && path === dbPath) return db;
  closeStore();
  mkdirSync(dirname(path), { recursive: true });
  db = new Database(path, { create: true });
  db.exec(`
    CREATE TABLE IF NOT EXISTS chats (
      chat_id INTEGER PRIMARY KEY,
      sid TEXT,
      model TEXT,
      agent TEXT
    );
    CREATE TABLE IF NOT EXISTS kv (
      key TEXT PRIMARY KEY,
      value TEXT
    );
  `);
  dbPath = path;
  return db;
}

export function closeStore(): void {
  try {
    db?.close();
  } catch {
    /* já fechado */
  }
  db = null;
  dbPath = "";
}

function conn(): Database {
  return db ?? openStore();
}

export function loadChats(): Map<number, { sid?: string | null; model?: string; agent?: string }> {
  const rows = conn().query("SELECT chat_id, sid, model, agent FROM chats").all() as ChatRow[];
  const out = new Map<number, { sid?: string | null; model?: string; agent?: string }>();
  for (const r of rows) {
    out.set(r.chat_id, {
      ...(r.sid ? { sid: r.sid } : {}),
      ...(r.model ? { model: r.model } : {}),
      ...(r.agent ? { agent: r.agent } : {}),
    });
  }
  return out;
}

export function saveChat(
  chatId: number,
  cfg: { sid?: string | null; model?: string; agent?: string },
): void {
  conn()
    .query(
      `INSERT INTO chats (chat_id, sid, model, agent)
       VALUES ($id, $sid, $model, $agent)
       ON CONFLICT(chat_id) DO UPDATE SET sid=excluded.sid, model=excluded.model, agent=excluded.agent`,
    )
    .run({
      $id: chatId,
      $sid: cfg.sid ?? null,
      $model: cfg.model ?? null,
      $agent: cfg.agent ?? null,
    });
}

export function kvGet(key: string): string | null {
  const row = conn().query("SELECT value FROM kv WHERE key = $k").get({ $k: key }) as {
    value: string;
  } | null;
  return row?.value ?? null;
}

export function kvSet(key: string, value: string): void {
  conn()
    .query("INSERT INTO kv (key, value) VALUES ($k, $v) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    .run({ $k: key, $v: value });
}
