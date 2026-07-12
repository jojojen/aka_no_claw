"""Bluetooth and IR Web capability adapter (P1 R1.5c, #74)."""
from __future__ import annotations
import logging
from typing import Protocol
from .command_bridge_models import STATUS_ERROR, STATUS_OK
logger = logging.getLogger(__name__)
_CHAT_ID = "web-bridge"
class HomeDeps(Protocol):
    def _run_command_raw(self, command: str, remainder: str, chat_id: str = _CHAT_ID): ...
    def _markup_to_actions(self, markup): ...
    def _callbacks(self) -> dict: ...
class HomeCapability:
    def __init__(self, deps: HomeDeps) -> None:
        self._deps = deps
    def command(self, kind: str, text: str = "") -> dict:
        remainder = (text or "").strip()
        token = f"/{kind}"
        if remainder.startswith(token):
            remainder = remainder[len(token):].strip()
        if kind == "bluetooth" and remainder.lower() == "scan":
            remainder = ""
        message,markup=self._deps._run_command_raw(token,remainder)
        return {"status":STATUS_OK,"message":message,"actions":self._deps._markup_to_actions(markup)}
    def action(self, kind: str, callback_data: str) -> dict:
        prefix,_,payload=(callback_data or "").partition(":")
        if prefix != kind:
            return {"status":STATUS_ERROR,"message":f"未知的{kind}動作：{callback_data}","actions":[]}
        handler=self._deps._callbacks().get(kind if kind=="ir" else "bt")
        if handler is None:
            return {"status":STATUS_ERROR,"message":f"{kind}功能尚未啟用。","actions":[]}
        try:
            result=handler(payload,"",_CHAT_ID)
        except Exception as exc:
            logger.exception("%s action failed cb=%s",kind,callback_data)
            return {"status":STATUS_ERROR,"message":f"動作執行失敗：{exc}","actions":[]}
        toast,new_text,markup=(list(result)+[None,None,None])[:3] if isinstance(result,tuple) else (result,None,None)
        return {"status":STATUS_OK,"message":str(new_text if new_text else toast or ""),"actions":self._deps._markup_to_actions(markup)}
