import { describe, expect, test } from "bun:test";
import {
  turnFold,
  turnNew,
  turnRenderRunning,
  turnRenderThink,
  turnSelectOption,
  turnSplit,
  turnSubmitForm,
  type TurnState,
} from "../src/workers.ts";

async function freshTurn(): Promise<TurnState> {
  const t = await turnNew(1);
  (t as Record<string, unknown>).sid = "ses_abc";
  return t;
}

describe("turn worker (python)", () => {
  test("fold acumula delta e termina no idle", async () => {
    let t = await freshTurn();
    let r = await turnFold(t, { type: "session.text.delta", data: { sessionID: "ses_abc", delta: "olá " } });
    expect(r.action).toBe("none");
    expect((r.turn as Record<string, unknown>).out_text).toBe("olá ");
    r = await turnFold(r.turn, { type: "session.idle", data: { sessionID: "ses_abc" } });
    expect(r.action).toBe("finish");
  }, 20_000);

  test("permissão gera teclado com callback curto", async () => {
    const t = await freshTurn();
    const r = await turnFold(t, {
      type: "permission.asked",
      data: { sessionID: "ses_abc", id: "p1", action: "bash", message: "rodar ls" },
    });
    expect(r.action).toBe("push_force");
    const rr = await turnRenderRunning(r.turn, "/tmp");
    expect(rr.text).toContain("Permissão pedida");
    expect(rr.keyboard[0][0].data).toBe("perm:p1:once");
  }, 20_000);

  test("form single-select responde via select", async () => {
    let t = await freshTurn();
    const form = {
      id: "f1",
      sessionID: "ses_abc",
      fields: [{ name: "env", title: "Ambiente", options: [{ label: "prod", value: "prod" }] }],
    };
    let r = await turnFold(t, { type: "form.created", data: { form } });
    t = (await turnSelectOption(r.turn, "f1", 0, 0)).turn;
    const sub = await turnSubmitForm(t, "f1");
    expect(sub.complete).toBe(true);
    expect(sub.answer).toEqual({ env: "prod" });
  }, 20_000);

  test("think traz tempo e HTML expansível", async () => {
    const t = await freshTurn();
    const text = await turnRenderThink(t, 65, "/tmp");
    expect(text).toContain("1m05s");
    expect(text).toContain("blockquote");
  }, 20_000);

  test("split respeita limite", async () => {
    const chunks = await turnSplit(Array(50).fill("x".repeat(100)).join("\n"), 1000);
    expect(chunks.length).toBeGreaterThan(1);
    for (const c of chunks) expect(c.length).toBeLessThanOrEqual(1000);
  }, 20_000);
});
