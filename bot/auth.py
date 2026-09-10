"""Autenticacao do dono e o chat de destino das notificacoes."""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from . import config

logger = logging.getLogger(__name__)


def _get_owner_chat() -> int | None:
    if config.CHAT_ID:
        return int(config.CHAT_ID)
    return config.OWNER_ID or None


def is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and user.id == config.OWNER_ID


async def reject_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.warning("Unauthorized: %s (id=%s)", user.username or user.first_name, user.id)
    await update.message.reply_text("Access denied.")
