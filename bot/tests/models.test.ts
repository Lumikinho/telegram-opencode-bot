import { describe, expect, test } from "bun:test";
import {
  MODELS_PER_PAGE,
  baseModelSpec,
  clampModelPage,
  formatModelRef,
  modelsHead,
  modelsKeyboardRows,
  modelsPages,
  modelsSlice,
  splitModelRef,
} from "../src/utils/models.ts";

const flat = (rows: { text: string; data: string }[][]) => rows.flat();

describe("modelsPages", () => {
  test("mínimo um", () => {
    expect(modelsPages(0)).toBe(1);
    expect(modelsPages(10)).toBe(1);
    expect(modelsPages(11)).toBe(2);
    expect(modelsPages(25)).toBe(3);
  });
});

describe("modelsKeyboardRows", () => {
  test("uma página sem navegação", () => {
    const rows = modelsKeyboardRows(["a/x", "b/y", "c/z"]);
    expect(flat(rows).every((b) => b.data.startsWith("mod:"))).toBe(true);
    expect(rows.length).toBe(2); // 2+1 por linha, sem navegação
  });

  test("paginação e navegação", () => {
    const models = Array.from({ length: 25 }, (_, i) => `prov/model${String(i).padStart(2, "0")}`);
    const first = modelsKeyboardRows(models, 0);
    expect(first.at(-1)!.map((b) => b.data)).toEqual(["mpg:1"]);
    expect(flat(first.slice(0, -1)).map((b) => b.text)).toEqual(models.slice(0, 10));

    const mid = modelsKeyboardRows(models, 1);
    expect(mid.at(-1)!.map((b) => b.data)).toEqual(["mpg:0", "mpg:2"]);

    const last = modelsKeyboardRows(models, 2);
    expect(last.at(-1)!.map((b) => b.data)).toEqual(["mpg:1"]);
    expect(flat(last.slice(0, -1)).map((b) => b.text)).toEqual(models.slice(20));
  });

  test("callbacks dentro do limite de 64 bytes do Telegram", () => {
    const models = Array.from({ length: 30 }, (_, i) => `provider-com-nome-longo/modelo-com-nome-longo-${i}`);
    for (let page = 0; page < 3; page++) {
      for (const b of flat(modelsKeyboardRows(models, page))) {
        expect(new TextEncoder().encode(b.data).length).toBeLessThanOrEqual(64);
      }
    }
  });

  test("página fora do intervalo limita", () => {
    const models = Array.from({ length: 12 }, (_, i) => `p/m${i}`);
    const hi = modelsKeyboardRows(models, 99);
    expect(hi.at(-1)!.map((b) => b.data)).toEqual(["mpg:0"]);
    const lo = modelsKeyboardRows(models, -5);
    expect(lo.at(-1)!.map((b) => b.data)).toEqual(["mpg:1"]);
  });

  test("1 botão por modelo, até 2 por linha", () => {
    const rows = modelsKeyboardRows(["a/1", "b/2", "c/3"], 0);
    expect(rows[0].length).toBe(2);
    expect(rows[1].length).toBe(1);
  });
});

describe("modelsSlice", () => {
  test("fatia a página limitada", () => {
    const models = Array.from({ length: 12 }, (_, i) => `p/m${i}`);
    expect(modelsSlice(models, 0)).toEqual(models.slice(0, 10));
    expect(modelsSlice(models, 1)).toEqual(models.slice(10));
    expect(modelsSlice(models, 99)).toEqual(models.slice(10));
  });
});

describe("clampModelPage", () => {
  test("limita ao intervalo", () => {
    expect(clampModelPage(99, 2)).toBe(1);
    expect(clampModelPage(-5, 2)).toBe(0);
    expect(clampModelPage(NaN, 2)).toBe(0);
  });
});

describe("modelsHead", () => {
  test("indica página só quando há várias", () => {
    expect(modelsHead(undefined, 0, 1)).not.toContain("página");
    expect(modelsHead(undefined, 0, 1)).toContain("à definir");
    const head = modelsHead("a/b", 1, 3);
    expect(head).toContain("página 2/3");
    expect(head).toContain("a/b");
  });
});

describe("MODELS_PER_PAGE", () => {
  test("10 como no núcleo Python", () => {
    expect(MODELS_PER_PAGE).toBe(10);
  });
});

describe("splitModelRef/formatModelRef", () => {
  test("sem variante", () => {
    expect(splitModelRef("opencode/muse-spark-1.3")).toEqual({ providerID: "opencode", modelID: "muse-spark-1.3" });
    expect(formatModelRef({ providerID: "opencode", modelID: "muse-spark-1.3" })).toBe("opencode/muse-spark-1.3");
  });
  test("com variante", () => {
    expect(splitModelRef("google/gemini-3.8-flash#high")).toEqual({ providerID: "google", modelID: "gemini-3.8-flash", variant: "high" });
    expect(formatModelRef({ providerID: "google", modelID: "gemini-3.8-flash", variant: "high" })).toBe("google/gemini-3.8-flash#high");
  });
  test("round-trip e base", () => {
    expect(formatModelRef(splitModelRef("a/b#c"))).toBe("a/b#c");
    expect(baseModelSpec("a/b#c")).toBe("a/b");
    expect(baseModelSpec("a/b")).toBe("a/b");
  });
});
