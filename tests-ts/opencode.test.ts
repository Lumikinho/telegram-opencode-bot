import { describe, expect, test } from "bun:test";
import { authHeader, HEALTH_PATHS, pickSessionId } from "../src/opencode.ts";

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
});

describe("HEALTH_PATHS", () => {
  test("cascata V2 → legado V1 → info, nessa ordem", () => {
    expect([...HEALTH_PATHS]).toEqual(["/api/health", "/global/health", "/api/status"]);
  });
});
