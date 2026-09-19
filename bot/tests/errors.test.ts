import { describe, expect, test } from "bun:test";
import { emptyAnswerHint, friendlyError } from "../src/utils/errors.ts";

describe("friendlyError", () => {
  test("payment -> custo/billing", () => {
    expect(friendlyError({ type: "provider.auth", message: "No payment method" })).toContain("[COST]");
  });
  test("500 interno -> transitório", () => {
    const m = friendlyError({ type: "provider.internal", message: "Internal server error", status: 500 });
    expect(m).toContain("500");
    expect(m).toContain("/models");
  });
  test("rate limit", () => {
    expect(friendlyError(new Error("Rate limit exceeded"))).toContain("[T]");
  });
  test("worker fora", () => {
    expect(friendlyError(new Error("worker /api/turns/fold HTTP 500"))).toContain("dev-worker");
  });
  test("sem JSON cru", () => {
    const m = friendlyError({ type: "provider.internal", message: "x" });
    expect(m).not.toContain("'type'");
  });
  test("socket fechado -> NET transitório", () => {
    const m = friendlyError(new TypeError("The socket connection was closed unexpectedly."));
    expect(m).toContain("[NET]");
    expect(m).not.toContain("verbose");
  });
  test("hint de resposta vazia", () => {
    expect(emptyAnswerHint()).toContain("/new");
  });
});
