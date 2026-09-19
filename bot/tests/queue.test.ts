import { describe, expect, test } from "bun:test";
import {
  clearQueue,
  dequeue,
  enqueue,
  listQueue,
  previewOf,
  queuePosition,
  queueSize,
  removeFromQueue,
  resetQueueIdCounterForTests,
  type QueueMap,
} from "../src/services/queue.ts";

describe("fila por chat", () => {
  test("enfileira e drena em ordem (bolo -> brownie)", () => {
    resetQueueIdCounterForTests();
    const map: QueueMap = new Map();
    const a = enqueue(map, 1, { sid: "s1", text: "faça um bolo" });
    const b = enqueue(map, 1, { sid: "s1", text: "faça um brownie" });
    expect(a.position).toBe(1);
    expect(b.position).toBe(2);
    expect(queueSize(map, 1)).toBe(2);
    expect(queuePosition(map, 1, b.item.id)).toBe(2);
    expect(dequeue(map, 1)?.text).toBe("faça um bolo");
    expect(dequeue(map, 1)?.text).toBe("faça um brownie");
    expect(queueSize(map, 1)).toBe(0);
  });

  test("filas são isoladas por chat", () => {
    const map: QueueMap = new Map();
    enqueue(map, 1, { sid: "s1", text: "bolo" });
    enqueue(map, 2, { sid: "s2", text: "brownie" });
    expect(queueSize(map, 1)).toBe(1);
    expect(queueSize(map, 2)).toBe(1);
    expect(listQueue(map, 1)[0]?.text).toBe("bolo");
  });

  test("cancelar um pedido do meio mantém ordem", () => {
    const map: QueueMap = new Map();
    const a = enqueue(map, 7, { sid: "s", text: "um" });
    const b = enqueue(map, 7, { sid: "s", text: "dois" });
    const c = enqueue(map, 7, { sid: "s", text: "três" });
    expect(removeFromQueue(map, 7, b.item.id)?.text).toBe("dois");
    expect(listQueue(map, 7).map((q) => q.text)).toEqual(["um", "três"]);
    expect(queuePosition(map, 7, c.item.id)).toBe(2);
    expect(removeFromQueue(map, 7, "inexistente")).toBeNull();
    expect(removeFromQueue(map, 7, a.item.id)).not.toBeNull();
    expect(clearQueue(map, 7).map((q) => q.text)).toEqual(["três"]);
    expect(queueSize(map, 7)).toBe(0);
  });

  test("previewOf resume sem quebrar", () => {
    expect(previewOf("  faça\n um   bolo  ")).toBe("faça um bolo");
    expect(previewOf("")).toBe("(sem texto)");
    expect(previewOf("x".repeat(100), 80).length).toBeLessThanOrEqual(80);
  });
});
