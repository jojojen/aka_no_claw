"""Web workflow capability adapter (P1 R1.5b, #74)."""
from __future__ import annotations
import logging
from typing import Protocol
from .command_bridge_models import STATUS_ERROR, STATUS_OK

logger = logging.getLogger(__name__)
_CHAT_ID = "web-workflow"

class WorkflowDeps(Protocol):
    def _workflow_surface(self) -> tuple[object, object]: ...
    def _workflow_create_arg(self, remainder: str) -> str | None: ...
    def _run_workflow_create_with_chat_backend(self, description: str, *, chat_backend: str | None) -> dict: ...
    def _markup_to_actions(self, markup): ...

class WorkflowCapability:
    def __init__(self, deps: WorkflowDeps) -> None:
        self._deps = deps
    def run_command(self, text: str, *, chat_backend: str | None = None) -> dict:
        handler, editor = self._deps._workflow_surface()
        raw = (text or "").strip()
        if editor.is_capturing(_CHAT_ID) and not raw.startswith("/"):
            try:
                captured = editor.handle_text_capture(raw, _CHAT_ID)
            except Exception as exc:
                logger.exception("workflow capture failed text=%r", raw)
                return {"status": STATUS_ERROR, "message": f"工作流欄位輸入失敗：{exc}", "actions": []}
            if captured is not None:
                message, markup = captured
                return {"status": STATUS_OK, "message": str(message or ""), "actions": self._deps._markup_to_actions(markup)}
        remainder = raw[len("/workflow"):].strip() if raw.startswith("/workflow") else raw
        create_arg = self._deps._workflow_create_arg(remainder)
        if chat_backend and create_arg is not None and not create_arg.lstrip().startswith("{"):
            return self._deps._run_workflow_create_with_chat_backend(create_arg, chat_backend=chat_backend)
        try:
            result = handler(remainder, _CHAT_ID)
        except Exception as exc:
            logger.exception("workflow command failed text=%r", text)
            return {"status": STATUS_ERROR, "message": f"工作流指令失敗：{exc}", "actions": []}
        message, markup = (result[0], result[1] if len(result) > 1 else None) if isinstance(result, tuple) else (result, None)
        return {"status": STATUS_OK, "message": str(message or ""), "actions": self._deps._markup_to_actions(markup)}
    def run_action(self, callback_data: str) -> dict:
        _, editor = self._deps._workflow_surface()
        prefix, _, payload = (callback_data or "").partition(":")
        if prefix != "wfe":
            return {"status": STATUS_ERROR, "message": f"未知的工作流動作：{callback_data}", "actions": []}
        handler = editor.callback_handlers().get("wfe")
        if handler is None:
            return {"status": STATUS_ERROR, "message": "工作流編輯器尚未啟用。", "actions": []}
        try:
            result = handler(payload, "", _CHAT_ID)
        except Exception as exc:
            logger.exception("workflow action failed cb=%s", callback_data)
            return {"status": STATUS_ERROR, "message": f"動作執行失敗：{exc}", "actions": []}
        toast, new_text, markup = (list(result)+[None,None,None])[:3] if isinstance(result, tuple) else (result,None,None)
        return {"status": STATUS_OK, "message": str(new_text if new_text else toast or ""), "actions": self._deps._markup_to_actions(markup)}
