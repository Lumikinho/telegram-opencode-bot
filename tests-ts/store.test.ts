import { afterEach, describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { closeStore, kvGet, kvSet, loadChats, openStore, saveChat } from "../src/store.ts";

let dir = "";

function freshDb(): string {
  dir = mkdtempSync(join(tmpdir(), "hybrid-store-"));
  const path = join(dir, "test.sqlite");
  process.env.HYBRID_DB = path;
  openStore(path);
  return path;
}

afterEach(() => {
  closeStore();
  delete process.env.HYBRID_DB;
  if (dir) rmSync(dir, { recursive: true, force: true });
  dir = "";
});

describe("bun:sqlite store", () => {
  test("salva e recarrega chats", () => {
    freshDb();
    saveChat(42, { sid: "ses_x", model: "anthropic/claude", agent: "dev" });
    saveChat(7, {});
    const chats = loadChats();
    expect(chats.get(42)).toEqual({ sid: "ses_x", model: "anthropic/claude", agent: "dev" });
    expect(chats.get(7)).toEqual({});
  });

  test("atualiza sid sem duplicar", () => {
    freshDb();
    saveChat(42, { sid: "old" });
    saveChat(42, { sid: "new", model: "m" });
    const chats = loadChats();
    expect(chats.size).toBe(1);
    expect(chats.get(42)).toEqual({ sid: "new", model: "m" });
  });

  test("kv round-trip", () => {
    freshDb();
    expect(kvGet("nope")).toBeNull();
    kvSet("last_boot", "abc");
    expect(kvGet("last_boot")).toBe("abc");
    kvSet("last_boot", "def");
    expect(kvGet("last_boot")).toBe("def");
  });
});
