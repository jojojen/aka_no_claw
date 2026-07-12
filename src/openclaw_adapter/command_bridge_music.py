"""Music capability adapter for the command bridge (P1 R1.5a, #74)."""

from __future__ import annotations

import logging
from typing import Protocol

from .command_bridge_models import STATUS_ERROR, STATUS_OK

logger = logging.getLogger(__name__)
_BRIDGE_CHAT_ID = "web-bridge"


class MusicDeps(Protocol):
    settings: object

    def _run_command_raw(self, command: str, remainder: str, chat_id: str = _BRIDGE_CHAT_ID): ...
    def _markup_to_actions(self, markup): ...
    def _callbacks(self) -> dict: ...
    def _run_list_action(self, prefix: str, payload: str) -> dict: ...


class MusicCapability:
    def __init__(self, deps: MusicDeps) -> None:
        self._deps = deps

    def run_command(self, text: str) -> dict:
        message, markup = self._deps._run_command_raw("/music", (text or "").strip())
        return {"status": STATUS_OK, "message": message, "actions": self._deps._markup_to_actions(markup)}

    def run_queue_command(self, text: str) -> dict:
        message, markup = self._deps._run_command_raw("/musicqueue", (text or "").strip())
        return {"status": STATUS_OK, "message": message, "actions": self._deps._markup_to_actions(markup)}

    def run_action(self, callback_data: str) -> dict:
        prefix, _, payload = (callback_data or "").partition(":")
        if prefix in ("pg", "del", "close"):
            return self._deps._run_list_action(prefix, payload)
        if prefix != "music":
            return {"status": STATUS_ERROR, "message": f"未知的音樂動作：{callback_data}", "actions": []}
        handler = self._deps._callbacks().get("music")
        if handler is None:
            return {"status": STATUS_ERROR, "message": "音樂功能尚未啟用。", "actions": []}
        try:
            result = handler(payload, "", _BRIDGE_CHAT_ID)
        except Exception as exc:  # noqa: BLE001
            logger.exception("music action failed cb=%s", callback_data)
            return {"status": STATUS_ERROR, "message": f"動作執行失敗：{exc}", "actions": []}
        toast, new_text, markup = (list(result) + [None, None, None])[:3] if isinstance(result, tuple) else (result, None, None)
        return {"status": STATUS_OK, "message": str(new_text if new_text else toast or ""), "actions": self._deps._markup_to_actions(markup)}

    def now_playing(self) -> dict:
        from . import music_command
        try:
            name = music_command.now_playing(self._deps.settings)
        except Exception:  # noqa: BLE001
            logger.exception("now_playing lookup failed")
            name = None
        return {"status": STATUS_OK, "name": name}
