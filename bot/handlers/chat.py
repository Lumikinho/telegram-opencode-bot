"""Mensagens livres e anexos -> turnos do opencode."""
import logging
from telegram import Update
from telegram.ext import ContextTypes
from ..auth import is_owner, reject_unauthorized
from ..turns import _consume_custom_answer, _media_entries, _media_to_file_parts, _start_turn


logger = logging.getLogger(__name__)


async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    if await _consume_custom_answer(update, text):
        return
    await _start_turn(update, text)


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    text = (update.message.caption or "").strip()
    if await _consume_custom_answer(update, text):
        return
    entries = _media_entries(update)
    if not entries and not text:
        await update.message.reply_text(
            "⚠️ Anexos suportados: foto, documento, áudio, voz, vídeo e GIF.\n"
            "Envie junto um texto ou legenda com a instrução.",
        )
        return
    parts = await _media_to_file_parts(context.bot, entries) if entries else None
    await _start_turn(update, text, parts=parts)
