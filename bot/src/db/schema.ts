/** Schema SQLite (bun:sqlite): persiste sessão/modelo/agente por chat. */
export const SCHEMA = `
CREATE TABLE IF NOT EXISTS chats (
  chat_id   INTEGER PRIMARY KEY,
  sid       TEXT,
  model     TEXT,
  agent     TEXT,
  updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
`;
