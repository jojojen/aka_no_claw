"""Goal-bridge glue for the aka Telegram processor.

Moved out of telegram_bot.py in R2.5 (#75). These functions carry the actual
goal-loop behavior (routing a natural-language goal, executing it through the
CommandBridge, rendering follow-up buttons, and the async callback worker) so
the processor's overrides shrink to thin hooks. The bridge and settings are
passed in explicitly; the processor keeps the lazy-init cache of the bridge.
"""

from __future__ import annotations

import logging
import threading

from assistant_runtime.logging_utils import trim_for_log
from telegram_nl.natural_language import TelegramNaturalLanguageIntent

from .command_bridge_models import STATUS_OK, WebCommandResponse, parse_request
from .llm_pool_settings import default_chat_backend

logger = logging.getLogger(__name__)


def ensure_goal_bridge(settings, existing):
    """Return the cached bridge, lazily constructing one from settings when
    absent. The processor stores the result so this stays a single-init cache."""
    if existing is None and settings is not None:
        from .command_bridge import CommandBridge

        return CommandBridge(settings=settings)
    return existing


def goal_conversation_id(chat_id: str | int) -> str:
    return f"telegram:{chat_id}"


def goal_chat_backend(settings) -> str:
    if settings is None:
        return "local"
    return default_chat_backend(settings)


def build_goal_bridge_request(text: str, chat_id: str | int, *, settings):
    return parse_request(
        {
            "mode": "chat",
            "input": text,
            "conversation_id": goal_conversation_id(chat_id),
            "chat_backend": goal_chat_backend(settings),
            "source": "telegram",
        }
    )


def route_goal_loop_intent(bridge, text: str, *, settings) -> TelegramNaturalLanguageIntent | None:
    if bridge is None:
        return None
    try:
        plan, _metadata = bridge._select_chat_tool_plan(build_goal_bridge_request(text, "router", settings=settings))
    except Exception:
        logger.exception("Telegram goal-loop router failed text=%s", trim_for_log(text, limit=240))
        return None
    if plan is None or plan.tool != "__goal__":
        return None
    return TelegramNaturalLanguageIntent(
        intent="execute_goal",
        workflow_description=plan.query,
        confidence=0.8,
    )


def execute_goal_bridge(bridge, text: str, chat_id: str | int, *, settings) -> WebCommandResponse:
    if bridge is None:
        return WebCommandResponse(status="error", message="goal bridge 尚未啟用。")
    return bridge.handle(build_goal_bridge_request(text, chat_id, settings=settings))


def run_goal_bridge(bridge, goal: str, chat_id: str | int, *, settings) -> WebCommandResponse:
    if bridge is None:
        return WebCommandResponse(status="error", message="goal bridge 尚未啟用。")
    req = build_goal_bridge_request(goal, chat_id, settings=settings)
    return bridge._run_goal_loop_blocking(req, goal, planner_metadata=None)


def goal_reply_markup(response: WebCommandResponse) -> dict[str, object] | None:
    if not response.actions:
        return None
    rows = []
    for action in response.actions:
        if action.command != "chat" or not action.input:
            continue
        rows.append([{"text": action.label, "callback_data": f"goal:{action.input}"}])
    if not rows:
        return None
    return {"inline_keyboard": rows}


def handle_goal_callback(
    bridge,
    payload: str,
    original_text: str,
    chat_id: str,
    *,
    settings,
) -> tuple[object, str | None, object]:
    response = execute_goal_bridge(bridge, payload, chat_id, settings=settings)
    if response.status != STATUS_OK:
        return response.message, None, None
    return None, response.message, goal_reply_markup(response)


def run_goal_callback_async(
    *,
    client,
    callback_id: str,
    chat_id: str,
    message_id: int,
    payload: str,
    original_text: str,
    bridge,
    settings,
) -> None:
    """Ack the callback immediately, then run the goal in a worker thread and
    edit the original message with the result (or an error notice)."""
    try:
        client.answer_callback_query(
            callback_query_id=callback_id,
            text="收到，正在處理…",
        )
    except Exception:
        logger.exception("answer_callback_query failed for async goal callback chat_id=%s", chat_id)

    def _worker() -> None:
        try:
            response = execute_goal_bridge(bridge, payload, chat_id, settings=settings)
            reply_markup = goal_reply_markup(response) if response.status == STATUS_OK else None
            client.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=response.message,
                reply_markup=reply_markup,
            )
        except Exception:
            logger.exception("async goal callback worker failed chat_id=%s payload=%s", chat_id, payload)
            try:
                client.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"{original_text}\n\n⚠️ 目標執行失敗，請稍後再試。",
                    reply_markup=None,
                )
            except Exception:
                logger.exception("async goal callback fallback edit failed chat_id=%s", chat_id)

    threading.Thread(target=_worker, daemon=True).start()
