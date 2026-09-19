import { describe, expect, test } from "bun:test";
import { baseName, formatExecResult, isAllowed, parseArgv, runExec } from "../src/services/exec.ts";

describe("exec allowlist", () => {
  test("parse respeita aspas", () => {
    expect(parseArgv('git log --oneline -n 5')).toEqual(["git", "log", "--oneline", "-n", "5"]);
    expect(parseArgv('echo "olá mundo"')).toEqual(["echo", "olá mundo"]);
    expect(parseArgv("echo 'a b' c")).toEqual(["echo", "a b", "c"]);
  });

  test("basename + allowlist", () => {
    expect(baseName("/usr/bin/git")).toBe("git");
    expect(isAllowed("git")).toBe(true);
    expect(isAllowed("/usr/bin/ls")).toBe(true);
    expect(isAllowed("rm")).toBe(false);
    expect(isAllowed("sudo")).toBe(false);
  });

  test("nega comando fora da lista sem spawnar", async () => {
    const r = await runExec("rm -rf /tmp/x", 0);
    expect(r.ok).toBe(false);
    expect(r.error).toContain("não permitido");
    expect(r.exitCode).toBeNull();
  });

  test("echo permitido roda confinado", async () => {
    const r = await runExec("echo hello-exec", 0, { cwd: "/tmp" });
    expect(r.ok).toBe(true);
    expect(r.exitCode).toBe(0);
    expect(r.stdout).toContain("hello-exec");
    const html = formatExecResult(r);
    expect(html).toContain("hello-exec");
    expect(html).toContain("exit=0");
  }, 15_000);

  test("sem shell: metachars viram args literais", async () => {
    const r = await runExec("echo a; echo INJECTED", 0, { cwd: "/tmp" });
    expect(r.ok).toBe(true);
    // sem bash -c, o ";" não separa comandos
    expect(r.stdout).toContain("a; echo INJECTED");
    expect(r.stdout.trim()).not.toBe("a\nINJECTED");
  }, 15_000);
});
