import { describe, expect, test } from "bun:test";
import { escHtml, parseRestartTarget, RESTART_LABELS } from "../src/services/restart.ts";

describe("parseRestartTarget", () => {
  test("sem arg cai em both", () => {
    expect(parseRestartTarget(undefined)).toBe("both");
    expect(parseRestartTarget("")).toBe("both");
  });
  test("aliases pt/en", () => {
    expect(parseRestartTarget("bot")).toBe("bot");
    expect(parseRestartTarget("server")).toBe("server");
    expect(parseRestartTarget("servidor")).toBe("server");
    expect(parseRestartTarget("srv")).toBe("server");
    expect(parseRestartTarget("ambos")).toBe("both");
    expect(parseRestartTarget("tudo")).toBe("both");
    expect(parseRestartTarget("ALL")).toBe("both");
  });
  test("desconhecido é null", () => {
    expect(parseRestartTarget("banana")).toBeNull();
  });
  test("labels pt", () => {
    expect(RESTART_LABELS.bot).toBe("o bot");
    expect(RESTART_LABELS.server).toBe("o servidor");
    expect(RESTART_LABELS.both).toBe("o bot + o servidor");
  });
  test("escHtml escapa & < >", () => {
    expect(escHtml("<a>&")).toBe("&lt;a&gt;&amp;");
    expect(escHtml(null)).toBe("");
  });
});
