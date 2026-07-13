"""Telegram credential guards shared by the poller, bridge and services."""

from assistant_runtime import AssistantSettings


def require_telegram_token(settings: AssistantSettings) -> str:
    token = settings.openclaw_telegram_bot_token
    if token is None:
        raise RuntimeError("Telegram bot token is missing. Put it in .env as OPENCLAW_TELEGRAM_BOT_TOKEN.")
    return token


def require_telegram_chat_id(settings: AssistantSettings) -> str:
    chat_id = settings.openclaw_telegram_chat_id
    if chat_id is None:
        raise RuntimeError("Telegram chat id is missing. Put it in .env as OPENCLAW_TELEGRAM_CHAT_ID.")
    return chat_id
