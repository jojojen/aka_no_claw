"""Aggregate low-risk control actions from existing surfaces (design §6).

Candidates come from the executable system state — the same music control
callbacks and learned IR buttons the 生活 surfaces already expose — never
from hand-written utterance/hotword lists (design §3.2, Rule G). Each
provider is fail-soft: a broken surface contributes no candidates instead
of failing the whole voice request (design §13.4).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from .models import RISK_LOW, VoiceActionDescriptor, VoiceUserContext

logger = logging.getLogger(__name__)

SURFACE_MUSIC = "music"
SURFACE_IR = "ir"

DISPATCH_MUSIC_CALLBACK = "music_callback"
DISPATCH_IR_SEND = "ir_send"

# The bounded low-risk subset of the music control keyboard
# (music_command menu): playback transport + volume. Browsing, favorites
# and device switching stay out of the voice shortlist.
_MUSIC_CONTROLS: tuple[tuple[str, str, str], ...] = (
    ("music.playpause", "暫停／繼續播放", "music:playpause"),
    ("music.next", "下一首", "music:next"),
    ("music.prev", "上一首", "music:prev"),
    ("music.stop", "停止播放", "music:stop"),
    ("music.louder", "音量提高", "music:louder"),
    ("music.lower", "音量降低", "music:lower"),
    ("music.mute", "靜音", "music:mute"),
)


class CompositeVoiceActionRegistry:
    """Registry over provider callables so the voice package stays free of
    bridge imports; the bridge supplies live availability/enumeration."""

    def __init__(
        self,
        *,
        music_available: Callable[[], bool],
        ir_buttons: Callable[[], Sequence[tuple[str, str]]],
    ) -> None:
        self._music_available = music_available
        self._ir_buttons = ir_buttons

    def list_actions(
        self, *, user_context: VoiceUserContext
    ) -> Sequence[VoiceActionDescriptor]:
        del user_context  # context ranking lands with PR2+ prototypes
        actions: list[VoiceActionDescriptor] = []
        actions.extend(self._music_actions())
        actions.extend(self._ir_actions())
        return tuple(actions)

    def _music_actions(self) -> list[VoiceActionDescriptor]:
        try:
            if not self._music_available():
                return []
        except Exception:  # noqa: BLE001 — fail-soft per surface
            logger.exception("voice registry: music availability probe failed")
            return []
        return [
            VoiceActionDescriptor(
                action_id=action_id,
                display_label=label,
                surface=SURFACE_MUSIC,
                risk=RISK_LOW,
                reversible=True,
                available=True,
                dispatch_kind=DISPATCH_MUSIC_CALLBACK,
                dispatch_payload={"callback_data": callback_data},
            )
            for action_id, label, callback_data in _MUSIC_CONTROLS
        ]

    def _ir_actions(self) -> list[VoiceActionDescriptor]:
        try:
            buttons = tuple(self._ir_buttons())
        except Exception:  # noqa: BLE001 — fail-soft per surface
            logger.exception("voice registry: IR button enumeration failed")
            return []
        return [
            VoiceActionDescriptor(
                action_id=f"ir.{device}.{button}",
                display_label=f"{device}／{button}",
                surface=SURFACE_IR,
                risk=RISK_LOW,
                reversible=False,
                available=True,
                dispatch_kind=DISPATCH_IR_SEND,
                dispatch_payload={"device": device, "button": button},
            )
            for device, button in buttons
        ]
