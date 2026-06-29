"""Live Chat/planner integration over the generated tool catalog (issue #52).

The reuse engine (param extraction, tool_type matching, lifecycle metrics) lives
in :class:`DynamicToolRunner`, but historically it was only reachable through the
explicit ``/new`` slash command. This module bridges the gap the issue's real
success criterion asks for: a *plain free-text* message that no built-in intent
understood should still find and reuse an existing generated tool.

Behaviour (confirmed with the user):

- A **promoted** (trusted) tool matches → run it immediately, answer inline.
- A **fresh** (candidate/recovering) tool matches → ask before reusing it, so a
  not-yet-proven tool never acts without consent.
- **No** usable tool but there was a relevance signal → offer to generate one;
  never auto-spin codegen, and never nag when there's no signal at all (the
  runner's lexical gate returns ``none`` for chatter).

The bot calls :meth:`handle_text` at its "no built-in intent matched" fallback;
the inline-button confirmations route back through the bot's callback registry
to :meth:`callback_handlers`."""
from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# How long a pending confirmation stays valid. Long enough for a human to read
# and tap, short enough that a stale token can't run a tool much later.
_PENDING_TTL_SECONDS = 600.0


@dataclass
class _Pending:
    kind: str  # "reuse" | "generate"
    text: str
    plan: object | None
    created_at: float


class CatalogPlanner:
    """Stateful glue between the bot's free-text fallback and the tool catalog.

    One instance per bot process; holds short-lived pending confirmations keyed
    by an opaque token embedded in inline-button ``callback_data``."""

    def __init__(self, runner) -> None:
        self._runner = runner
        self._pending: dict[str, _Pending] = {}

    # ---- free-text entry point --------------------------------------------

    def handle_text(self, text: str, chat_id: str) -> "tuple[str, dict | None] | None":
        """Decide how to respond to an unmatched free-text message.

        Returns ``(reply, reply_markup)`` to answer (markup may be ``None``), or
        ``None`` to defer to the bot's default "unknown command" reply."""
        if self._runner is None or not (text or "").strip():
            return None
        try:
            plan = self._runner.plan_for_text(text)
        except Exception:
            logger.exception("catalog_planner: plan_for_text failed")
            return None

        action = getattr(plan, "action", "none")
        if action == "none":
            return None
        if action == "run":
            try:
                reply = self._runner.run_reuse_plan(plan)
            except Exception:
                logger.exception("catalog_planner: promoted auto-run failed")
                return None
            return (reply, None)
        if action == "confirm_reuse":
            token = self._store(_Pending("reuse", text, plan, time.time()))
            label = getattr(plan, "tool_type", None) or "既有工具"
            kb = {
                "inline_keyboard": [[
                    {"text": f"✅ 使用「{label}」", "callback_data": f"cataloguse:{token}"},
                    {"text": "✖️ 取消", "callback_data": f"catalogno:{token}"},
                ]]
            }
            return (
                f"我找到一個既有工具「{label}」也許能回答這個需求（剛生成、尚未充分驗證）。"
                "要用它來回答嗎？",
                kb,
            )
        if action == "confirm_generate":
            token = self._store(_Pending("generate", text, None, time.time()))
            kb = {
                "inline_keyboard": [[
                    {"text": "🛠 新生成工具", "callback_data": f"catalognew:{token}"},
                    {"text": "✖️ 取消", "callback_data": f"catalogno:{token}"},
                ]]
            }
            return ("我沒有找到類似的既有工具。要我為你新生成一個嗎？", kb)
        return None

    # ---- callback registry ------------------------------------------------

    def callback_handlers(self) -> dict:
        """Prefix → handler map to merge into the bot's callback registry. Each
        handler returns ``(toast, new_text, new_reply_markup)``; a non-None
        ``new_text`` makes the bot edit the prompt message into the answer."""
        return {
            "cataloguse": self._cb_use,
            "catalognew": self._cb_generate,
            "catalogno": self._cb_cancel,
        }

    def _cb_use(self, payload: str, original_text: str, chat_id: str):
        pending = self._take(payload, "reuse")
        if pending is None:
            return ("這個確認已逾時，請重新輸入需求。", None, None)
        try:
            reply = self._runner.run_reuse_plan(pending.plan)
        except Exception:
            logger.exception("catalog_planner: confirmed reuse failed")
            return ("執行失敗，請看 log。", None, None)
        return (None, reply, None)

    def _cb_generate(self, payload: str, original_text: str, chat_id: str):
        pending = self._take(payload, "generate")
        if pending is None:
            return ("這個確認已逾時，請重新輸入需求。", None, None)
        try:
            reply = self._runner.run(pending.text)
        except Exception:
            logger.exception("catalog_planner: confirmed generate failed")
            return ("執行失敗，請看 log。", None, None)
        return (None, reply, None)

    def _cb_cancel(self, payload: str, original_text: str, chat_id: str):
        self._pending.pop(payload, None)
        return (None, "已取消。", None)

    # ---- pending store ----------------------------------------------------

    def _store(self, pending: _Pending) -> str:
        self._gc()
        token = secrets.token_urlsafe(8)
        self._pending[token] = pending
        return token

    def _take(self, token: str, kind: str) -> "_Pending | None":
        pending = self._pending.pop(token, None)
        if pending is None or pending.kind != kind:
            return None
        if time.time() - pending.created_at > _PENDING_TTL_SECONDS:
            return None
        return pending

    def _gc(self) -> None:
        now = time.time()
        stale = [t for t, p in self._pending.items()
                 if now - p.created_at > _PENDING_TTL_SECONDS]
        for t in stale:
            self._pending.pop(t, None)
