import { describe, expect, test } from "bun:test";
import { extractSection, startupMarkdown } from "../src/utils/version.ts";

const MD = `# Changelog

## [2.0.0-hybrid.1] - 2026-09-14

- Foo novo
- Bar novo

## [2.0.0-hybrid.0] - 2026-09-14

- Esqueleto
`;

describe("extractSection", () => {
  test("acha a seção da versão", () => {
    const sec = extractSection(MD, "2.0.0-hybrid.1");
    expect(sec.title).toContain("2.0.0-hybrid.1");
    expect(sec.body).toContain("Foo novo");
    expect(sec.body).not.toContain("Esqueleto");
  });

  test("cai na primeira seção quando a versão não existe", () => {
    const sec = extractSection(MD, "9.9.9");
    expect(sec.title).toContain("2.0.0-hybrid.1");
  });

  test("sem seções retorna vazio", () => {
    expect(extractSection("só texto", "1.0")).toEqual({ title: "", body: "" });
  });
});

describe("startupMarkdown", () => {
  test("monta versão, branch e changelog", () => {
    const md = startupMarkdown({
      version: "2.0.0-hybrid.1",
      branch: "bun-ts-python",
      commit: "abc123",
      changelogTitle: "[2.0.0-hybrid.1]",
      changelog: "- Foo novo",
    });
    expect(md).toContain("2.0.0-hybrid.1");
    expect(md).toContain("bun-ts-python");
    expect(md).toContain("abc123");
    expect(md).toContain("Foo novo");
  });

  test("sem commit nem changelog ainda monta", () => {
    const md = startupMarkdown({ version: "x", branch: "b", commit: "", changelogTitle: "", changelog: "" });
    expect(md).toContain("`x`");
    expect(md).toContain("`b`");
  });
});
