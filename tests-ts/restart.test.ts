import { describe, expect, test } from "bun:test";
import { parseRestartTarget, RESTART_LABELS } from "../src/restart.ts";
import { turnMediaNote, turnSafeFilename } from "../src/workers.ts";

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
});

describe("media worker (python)", () => {
  test("safe_filename limpa e cai no padrão", async () => {
    expect(await turnSafeFilename("nota: final?.pdf", "x.bin")).toBe("nota_ final_.pdf");
    expect(await turnSafeFilename("", "doc.bin")).toBe("doc.bin");
  }, 20_000);

  test("media_note oversize/inacessível", async () => {
    expect(await turnMediaNote("oversize", "v.mp4", 25)).toBe("[anexo ignorado (25 MiB, limite 20 MiB): v.mp4]");
    expect(await turnMediaNote("inaccessible", "a.ogg")).toBe("[anexo não acessível: a.ogg]");
  }, 20_000);
});
