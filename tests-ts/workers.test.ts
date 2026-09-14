import { describe, expect, test } from "bun:test";
import { batteryOnce, funnelStatus, renderMarkdown } from "../src/workers.ts";

describe("py workers", () => {
  test("battery worker responde JSON com formatted", async () => {
    const b = await batteryOnce("/sys/class/power_supply/battery");
    expect(typeof b.formatted).toBe("string");
    expect(b.formatted).toContain("Bateria");
  }, 20_000);

  test("funnel parse via worker (status nunca quebra)", async () => {
    const s = await funnelStatus();
    expect(Array.isArray(s.funnels)).toBe(true);
    expect(typeof s.raw).toBe("string");
  }, 20_000);

  test("render worker escapa HTML e mantém code", async () => {
    const html = await renderMarkdown("**oi** `x`");
    expect(html).toContain("<b>oi</b>");
    expect(html).toContain("<code>x</code>");
  }, 20_000);
});
