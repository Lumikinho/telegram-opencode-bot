/** Lista de modelos com botões + paginação (port de `bot/render.py`).
 *
 * `/models` mostra todos os modelos como botões clicáveis (`mod:<spec>`)
 * com navegação por páginas (`mpg:<pagina>`). Callbacks ficam bem abaixo
 * do limite de 64 bytes do Telegram.
 */

export const MODELS_PER_PAGE = 10;

export interface ModelButton {
  text: string;
  data: string;
}

/** Quantas páginas tem uma lista de `n` itens (mínimo 1). */
export function modelsPages(n: number, perPage: number = MODELS_PER_PAGE): number {
  return Math.max(1, Math.ceil(n / perPage));
}

/** Limita a página ao intervalo válido [0, total-1]. */
export function clampModelPage(page: number, total: number): number {
  if (!Number.isFinite(page)) return 0;
  return Math.max(0, Math.min(Math.floor(page), total - 1));
}

/** Fatia da página atual (já limitada). */
export function modelsSlice(models: string[], page: number, perPage: number = MODELS_PER_PAGE): string[] {
  const total = modelsPages(models.length, perPage);
  const p = clampModelPage(page, total);
  return models.slice(p * perPage, (p + 1) * perPage);
}

/** Linhas de botões: até 2 modelos por linha + navegação `mpg:<pagina>`.
 * Sem navegação quando há só 1 página (igual ao núcleo Python). */
export function modelsKeyboardRows(
  models: string[],
  page: number = 0,
  perPage: number = MODELS_PER_PAGE,
): ModelButton[][] {
  const total = modelsPages(models.length, perPage);
  const p = clampModelPage(page, total);
  const chunk = models.slice(p * perPage, (p + 1) * perPage);
  const rows: ModelButton[][] = [];
  let pending: ModelButton[] = [];
  for (const spec of chunk) {
    pending.push({ text: spec, data: `mod:${spec}` });
    if (pending.length === 2) {
      rows.push(pending);
      pending = [];
    }
  }
  if (pending.length) rows.push(pending);
  if (total > 1) {
    const nav: ModelButton[] = [];
    if (p > 0) nav.push({ text: "« Anterior", data: `mpg:${p - 1}` });
    if (p < total - 1) nav.push({ text: "Próxima »", data: `mpg:${p + 1}` });
    rows.push(nav);
  }
  return rows;
}

/** Cabeçalho do /models, com indicador de página só quando há mais de uma. */
export function modelsHead(current: string | null | undefined, page: number, total: number): string {
  const raw = current ?? "à definir";
  const spec = raw.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const suffix = total > 1 ? ` (página ${page + 1}/${total})` : "";
  return `<b>Modelo atual:</b> <code>${spec}</code>\n\nEscolha o modelo${suffix}:`;
}

/** Ref `provedor/modelo#variante` (variante opcional) para o POST /model. */
export interface ModelRef {
  providerID: string;
  modelID: string;
  variant?: string;
}

/** "p/m#v" -> {providerID: p, modelID: m, variant: v}. Sem "#" = sem variante. */
export function splitModelRef(spec: string): ModelRef {
  const [providerID, ...rest] = (spec ?? "").split("/");
  const joined = rest.join("/");
  const hash = joined.indexOf("#");
  if (hash < 0) return { providerID, modelID: joined };
  return { providerID, modelID: joined.slice(0, hash), variant: joined.slice(hash + 1) || undefined };
}

/** Inverso: {p, m, v} -> "p/m#v" (ou "p/m" sem variante). */
export function formatModelRef(ref: ModelRef): string {
  const base = `${ref.providerID}/${ref.modelID}`;
  return ref.variant ? `${base}#${ref.variant}` : base;
}

/** "p/m#v" -> "p/m" (base para buscar variantes no catálogo). */
export function baseModelSpec(spec: string): string {
  const { providerID, modelID } = splitModelRef(spec ?? "");
  return `${providerID}/${modelID}`;
}
