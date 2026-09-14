/** Anexos do Telegram -> partes `file` do opencode (port de turns.py/chat.py).
 *
 * Tipos: foto, documento, áudio, voz, vídeo, vídeo redondo e GIF.
 * Limite 20 MiB por arquivo; acima disso (ou sem acesso) vira nota de texto.
 */
import type { Bot, Context } from "grammy";
import { BOT_TOKEN } from "./config.ts";
import { turnCall } from "./workers.ts";

export const MEDIA_MAX_BYTES = 20 * 1024 * 1024;

const FALLBACK_MIME: Record<string, string> = {
  document: "application/octet-stream",
  audio: "audio/mpeg",
  voice: "audio/ogg",
  video: "video/mp4",
  video_note: "video/mp4",
  animation: "video/mp4",
  photo: "image/jpeg",
};

export interface MediaEntry {
  fileId: string;
  filename: string;
  mime: string;
}

export type PromptPart =
  | { type: "file"; url: string; filename?: string }
  | { type: "text"; text: string };

async function safeFilename(name: string, fallback: string): Promise<string> {
  const r = (await turnCall({ action: "safe_filename", name, default: fallback })) as {
    filename: string;
  };
  return r.filename;
}

async function mediaNote(kind: string, filename: string, sizeMb = 0): Promise<string> {
  const r = (await turnCall({ action: "media_note", kind, filename, size_mb: sizeMb })) as {
    text: string;
  };
  return r.text;
}

/** Extrai (file_id, filename, mime) de cada anexo da mensagem. */
export async function collectMedia(ctx: Context): Promise<MediaEntry[]> {
  const msg = ctx.message;
  if (!msg) return [];
  const out: MediaEntry[] = [];
  const m = msg as unknown as Record<string, unknown>;
  const photos = m.photo as { file_id: string; file_size?: number }[] | undefined;
  if (photos?.length) {
    const best = photos.reduce((a, b) => ((b.file_size ?? 0) > (a.file_size ?? 0) ? b : a));
    out.push({ fileId: best.file_id, filename: "photo.jpg", mime: "image/jpeg" });
  }
  for (const kind of ["document", "audio", "voice", "video", "video_note", "animation"] as const) {
    const f = m[kind] as { file_id: string; file_name?: string; mime_type?: string } | undefined;
    if (!f) continue;
    out.push({
      fileId: f.file_id,
      filename: await safeFilename(f.file_name ?? "", `${kind}.bin`),
      mime: f.mime_type ?? FALLBACK_MIME[kind],
    });
  }
  return out;
}

/** Baixa cada anexo e converte em parte `file` (data URL base64). */
export async function downloadParts(bot: Bot, entries: MediaEntry[]): Promise<PromptPart[]> {
  const parts: PromptPart[] = [];
  for (const e of entries) {
    let filePath: string | undefined;
    try {
      const f = await bot.api.getFile(e.fileId);
      filePath = f.file_path;
    } catch {
      parts.push({ type: "text", text: await mediaNote("inaccessible", e.filename) });
      continue;
    }
    if (!filePath) {
      parts.push({ type: "text", text: await mediaNote("inaccessible", e.filename) });
      continue;
    }
    let buf: ArrayBuffer;
    try {
      const res = await fetch(`https://api.telegram.org/file/bot${BOT_TOKEN}/${filePath}`);
      if (!res.ok) throw new Error(`file HTTP ${res.status}`);
      buf = await res.arrayBuffer();
    } catch {
      parts.push({ type: "text", text: await mediaNote("download_failed", e.filename) });
      continue;
    }
    if (buf.byteLength > MEDIA_MAX_BYTES) {
      parts.push({
        type: "text",
        text: await mediaNote("oversize", e.filename, Math.floor(buf.byteLength / (1024 * 1024))),
      });
      continue;
    }
    const b64 = Buffer.from(buf).toString("base64");
    parts.push({ type: "file", url: `data:${e.mime};base64,${b64}`, filename: e.filename });
  }
  return parts;
}

export async function captionOf(ctx: Context): Promise<string> {
  const msg = ctx.message as { caption?: string } | undefined;
  return (msg?.caption ?? "").trim();
}
