import { describe, expect, test } from "bun:test";
import { authHeader, HEALTH_PATHS, latestAssistantText, pickSessionId } from "../src/services/opencode.ts";

describe("v2 auth header", () => {
  test("monta Basic opencode:<senha>", () => {
    const h = authHeader("segredo");
    expect(h.Authorization).toBe(`Basic ${Buffer.from("opencode:segredo").toString("base64")}`);
  });
  test("vazio sem senha", () => {
    expect(authHeader("")).toEqual({});
  });
});

describe("pickSessionId", () => {  test("mantém sid guardado quando existe", () => {
    const sessions = [{ id: "a" }, { id: "b" }];
    expect(pickSessionId("b", sessions)).toBe("b");
  });
  test("pega a mais recente quando o guardado sumiu", () => {
    const sessions = [
      { id: "old", time: { updated: 1 } },
      { id: "new", time: { updated: 2 } },
    ];
    expect(pickSessionId("gone", sessions)).toBe("new");
  });
  test("null sem sessões", () => {
    expect(pickSessionId("x", [])).toBeNull();
  });
  test("com known: ignora sessão estranha mesmo sendo a mais recente", () => {
    const sessions = [
      { id: "mine", time: { updated: 1 } },
      { id: "foreign", time: { updated: 999 } },
    ];
    expect(pickSessionId("gone", sessions, new Set(["mine"]))).toBe("mine");
  });
  test("com known vazio: não adota nada (cria nova)", () => {
    const sessions = [{ id: "a", time: { updated: 5 } }];
    expect(pickSessionId("gone", sessions, new Set())).toBeNull();
  });
});

describe("HEALTH_PATHS", () => {
  test("cascata V2 → legado V1 → info, nessa ordem", () => {
    expect([...HEALTH_PATHS]).toEqual(["/api/health", "/global/health", "/api/status"]);
  });
});

describe("latestAssistantText", () => {
  const asst = (...texts: string[]) => ({
    type: "assistant",
    content: texts.map((text) => ({ type: "text" as const, text })),
  });
  test("pega o primeiro da página desc", () => {
    expect(latestAssistantText([asst("novo"), { type: "user" }, asst("velho")])).toBe("novo");
  });
  test("pula vazios e não-texto", () => {
    expect(
      latestAssistantText([
        { type: "assistant", content: [{ type: "text", text: "   " }] },
        { type: "assistant", content: [{ type: "tool", tool: "read" }] },
        asst("achou"),
      ]),
    ).toBe("achou");
  });
  test("sem assistant retorna vazio", () => {
    expect(latestAssistantText([{ type: "user" }])).toBe("");
    expect(latestAssistantText([])).toBe("");
    expect(latestAssistantText(null)).toBe("");
  });
  test("respeita limit", () => {
    expect(latestAssistantText([asst("abcdefgh")], 4)).toBe("abcd");
  });
});
