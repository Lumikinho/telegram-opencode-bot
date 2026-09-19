/** Fila por chat: estrutura pura (sem Telegram), testável.
 *
 * Uso: `turns.ts` mantém `Map<chatId, QueuedItem[]>` e usa estas funções.
 * Exemplo do usuário: "faça um bolo" (ativo) + "faça um brownie" (na fila,
 * com opção de cancelar).
 */

export interface QueuedFile {
  type: "file";
  url: string;
  filename?: string;
}

export interface QueuedPayload {
  sid: string;
  text: string;
  model?: string;
  agent?: string;
  files?: QueuedFile[];
}

export interface QueuedItem extends QueuedPayload {
  id: string;
  chatId: number;
  createdAt: number;
}

export type QueueMap = Map<number, QueuedItem[]>;

let counter = 0;

export function makeQueueId(): string {
  counter += 1;
  const rand = Math.random().toString(36).slice(2, 7);
  return `q${Date.now().toString(36)}${counter.toString(36)}${rand}`;
}

export function resetQueueIdCounterForTests(): void {
  counter = 0;
}

/** Prévia curta para exibir na fila (sem quebrar HTML). */
export function previewOf(text: string, max = 80): string {
  const oneLine = (text ?? "").replace(/\s+/g, " ").trim();
  if (!oneLine) return "(sem texto)";
  return oneLine.length > max ? `${oneLine.slice(0, max - 1)}…` : oneLine;
}

export function enqueue(
  map: QueueMap,
  chatId: number,
  payload: QueuedPayload,
  id = makeQueueId(),
): { item: QueuedItem; position: number } {
  const item: QueuedItem = {
    ...payload,
    files: payload.files ? [...payload.files] : undefined,
    id,
    chatId,
    createdAt: Date.now(),
  };
  let list = map.get(chatId);
  if (!list) {
    list = [];
    map.set(chatId, list);
  }
  list.push(item);
  return { item, position: list.length };
}

export function dequeue(map: QueueMap, chatId: number): QueuedItem | undefined {
  const list = map.get(chatId);
  if (!list?.length) return undefined;
  const item = list.shift();
  if (!list.length) map.delete(chatId);
  return item;
}

export function removeFromQueue(map: QueueMap, chatId: number, id: string): QueuedItem | null {
  const list = map.get(chatId);
  if (!list?.length) return null;
  const idx = list.findIndex((q) => q.id === id);
  if (idx < 0) return null;
  const [item] = list.splice(idx, 1);
  if (!list.length) map.delete(chatId);
  return item ?? null;
}

export function listQueue(map: QueueMap, chatId: number): QueuedItem[] {
  return [...(map.get(chatId) ?? [])];
}

export function queueSize(map: QueueMap, chatId: number): number {
  return map.get(chatId)?.length ?? 0;
}

/** Posição 1-based na fila, ou 0 se não está na fila. */
export function queuePosition(map: QueueMap, chatId: number, id: string): number {
  const list = map.get(chatId) ?? [];
  const idx = list.findIndex((q) => q.id === id);
  return idx < 0 ? 0 : idx + 1;
}

export function clearQueue(map: QueueMap, chatId: number): QueuedItem[] {
  const list = map.get(chatId) ?? [];
  map.delete(chatId);
  return [...list];
}
