"""Admin-only guard for the Telegram bot."""
from __future__ import annotations

from telegram import Update

from ads_agent.config import settings


def is_admin(update: Update) -> bool:
    if update.effective_user is None:
        return False
    return update.effective_user.id in settings().admin_telegram_ids
