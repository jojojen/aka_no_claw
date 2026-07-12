"""Unit tests for the voice-intent gate package (issue #82 PR1)."""

from __future__ import annotations

from openclaw_adapter.voice import (
    CompositeVoiceActionRegistry,
    VoiceActionDescriptor,
    VoiceIntentGate,
    VoiceUserContext,
)
from openclaw_adapter.voice import policy
from openclaw_adapter.voice.action_registry import (
    DISPATCH_IR_SEND,
    DISPATCH_MUSIC_CALLBACK,
    _MUSIC_CONTROLS,
)
from openclaw_adapter.voice.models import RISK_HIGH, RISK_LOW


# --- policy.is_short_form ---------------------------------------------------
def test_short_form_accepts_short_control_utterance():
    assert policy.is_short_form("關鍵善") is True
    assert policy.is_short_form("關電扇", duration_ms=1450) is True


def test_short_form_rejects_long_transcript():
    assert policy.is_short_form("幫我查一下明天東京的天氣還有匯率走勢如何") is False


def test_short_form_rejects_empty_and_url():
    assert policy.is_short_form("") is False
    assert policy.is_short_form("http://x.io") is False
    assert policy.is_short_form("看 www.a.io") is False


def test_short_form_rejects_long_audio_even_if_text_short():
    # Mumbled long speech can collapse to few STT chars; duration exposes it.
    assert policy.is_short_form("關鍵善", duration_ms=9000) is False


# --- CompositeVoiceActionRegistry -------------------------------------------
def _registry(music=True, ir=(), music_raises=False, ir_raises=False):
    def _music():
        if music_raises:
            raise RuntimeError("boom")
        return music

    def _ir():
        if ir_raises:
            raise RuntimeError("boom")
        return ir

    return CompositeVoiceActionRegistry(music_available=_music, ir_buttons=_ir)


def test_registry_lists_music_controls_when_available():
    actions = _registry(music=True).list_actions(user_context=VoiceUserContext())
    music = [a for a in actions if a.surface == "music"]
    assert len(music) == len(_MUSIC_CONTROLS)
    assert all(a.risk == RISK_LOW and a.available for a in music)
    assert all(a.dispatch_kind == DISPATCH_MUSIC_CALLBACK for a in music)
    assert {a.action_id for a in music} >= {"music.playpause", "music.next"}


def test_registry_skips_music_when_unavailable():
    actions = _registry(music=False).list_actions(user_context=VoiceUserContext())
    assert [a for a in actions if a.surface == "music"] == []


def test_registry_lists_learned_ir_buttons():
    actions = _registry(
        music=False, ir=(("fan", "power"), ("ac", "off"))
    ).list_actions(user_context=VoiceUserContext())
    assert [a.action_id for a in actions] == ["ir.fan.power", "ir.ac.off"]
    assert all(a.dispatch_kind == DISPATCH_IR_SEND for a in actions)
    assert actions[0].dispatch_payload == {"device": "fan", "button": "power"}


def test_registry_is_fail_soft_per_surface():
    actions = _registry(
        music_raises=True, ir=(("fan", "power"),)
    ).list_actions(user_context=VoiceUserContext())
    assert [a.action_id for a in actions] == ["ir.fan.power"]
    assert (
        _registry(music=True, ir_raises=True).list_actions(
            user_context=VoiceUserContext()
        )
        != ()
    )


# --- VoiceIntentGate ---------------------------------------------------------
def _gate(music=True, ir=()):
    return VoiceIntentGate(_registry(music=music, ir=ir))


def test_gate_clarifies_short_voice_open_tool_with_candidates():
    assert (
        _gate().should_clarify_before_open_tool(
            transcript="關鍵善", plan_query="關鍵善", duration_ms=1450
        )
        is True
    )


def test_gate_does_not_clarify_after_user_declined():
    assert (
        _gate().should_clarify_before_open_tool(
            transcript="關鍵善", plan_query="關鍵善", clarification_declined=True
        )
        is False
    )


def test_gate_does_not_clarify_long_form_question():
    assert (
        _gate().should_clarify_before_open_tool(
            transcript="幫我查明天東京的天氣以及需不需要帶傘",
            plan_query="東京 明天 天氣",
        )
        is False
    )


def test_gate_does_not_clarify_when_query_is_information_rich():
    # Short utterance but the router expanded a long, informative query:
    # treat as a real information ask, not an unresolved control.
    assert (
        _gate().should_clarify_before_open_tool(
            transcript="日圓匯率",
            plan_query="日圓 兌 新台幣 匯率 今天 走勢 分析",
        )
        is False
    )


def test_gate_does_not_clarify_without_candidates():
    assert (
        _gate(music=False).should_clarify_before_open_tool(
            transcript="關鍵善", plan_query="關鍵善"
        )
        is False
    )


def test_gate_ignores_non_low_risk_candidates():
    class _HighRiskRegistry:
        def list_actions(self, *, user_context):
            return (
                VoiceActionDescriptor(
                    action_id="danger.wipe",
                    display_label="全部刪除",
                    surface="test",
                    risk=RISK_HIGH,
                    reversible=False,
                    available=True,
                ),
            )

    gate = VoiceIntentGate(_HighRiskRegistry())
    assert (
        gate.should_clarify_before_open_tool(transcript="關鍵善", plan_query="關鍵善")
        is False
    )


def test_clarification_contract_shape_and_cap():
    many_ir = tuple((f"dev{i}", "power") for i in range(10))
    gate = _gate(music=True, ir=many_ir)
    clarification = gate.build_first_use_clarification(transcript="關鍵善")
    payload = clarification.to_dict()
    assert payload["kind"] == "clarify"
    assert payload["transcript"] == "關鍵善"
    assert payload["reason_code"] == policy.REASON_FIRST_USE_CONTROL_SUSPICION
    assert payload["fallback"] == {"label": policy.CLARIFY_FALLBACK_LABEL}
    candidates = payload["candidates"]
    assert 0 < len(candidates) <= policy.MAX_CLARIFY_CANDIDATES
    for c in candidates:
        assert set(c) == {"action_id", "display_label", "risk", "score"}
        assert c["risk"] == RISK_LOW


def test_gate_fail_soft_when_registry_raises():
    class _BrokenRegistry:
        def list_actions(self, *, user_context):
            raise RuntimeError("store corrupted")

    gate = VoiceIntentGate(_BrokenRegistry())
    assert (
        gate.should_clarify_before_open_tool(transcript="關鍵善", plan_query="關鍵善")
        is False
    )


# --- direct fast path (#82 PR4, §8.3) ----------------------------------------
from types import SimpleNamespace  # noqa: E402

from openclaw_adapter.voice.intent_gate import resolve_direct_prototype_action  # noqa: E402


def _descriptor(action_id="music.playpause", *, risk=RISK_LOW, reversible=True,
                available=True):
    return VoiceActionDescriptor(
        action_id=action_id,
        display_label=f"label:{action_id}",
        surface="music",
        risk=risk,
        reversible=reversible,
        available=available,
        dispatch_kind=DISPATCH_MUSIC_CALLBACK,
        dispatch_payload={"callback_data": "music:playpause"},
    )


def _proto(action_id="music.playpause", *, embedding=(1.0, 0.0), confirmed=3,
           prototype_id="p1"):
    return SimpleNamespace(
        action_id=action_id,
        embedding=tuple(embedding),
        confirmed_count=confirmed,
        prototype_id=prototype_id,
    )


def test_direct_resolves_mature_high_confidence_match():
    direct = resolve_direct_prototype_action(
        embedding=[1.0, 0.0],
        prototypes=[_proto()],
        actions=[_descriptor()],
    )
    assert direct is not None
    assert direct.action_id == "music.playpause"
    assert direct.prototype_id == "p1"
    assert direct.confidence >= policy.DIRECT_SIMILARITY_THRESHOLD
    assert direct.reason_code == policy.REASON_PROTOTYPE_HIGH_CONFIDENCE
    payload = direct.to_dict()
    assert payload["kind"] == "direct_action"
    assert payload["action"]["action_id"] == "music.playpause"
    assert payload["prototype_id"] == "p1"


def test_direct_rejects_below_similarity_threshold():
    # Orthogonal vectors: similarity 0 — open-set unknown speech (§3.4).
    assert resolve_direct_prototype_action(
        embedding=[0.0, 1.0],
        prototypes=[_proto()],
        actions=[_descriptor()],
    ) is None


def test_direct_rejects_small_top1_top2_margin():
    # Two different actions with near-identical scores must clarify, not direct.
    close = (0.9995, 0.0316)  # cosine vs [1,0] ≈ 0.9995
    assert resolve_direct_prototype_action(
        embedding=[1.0, 0.0],
        prototypes=[
            _proto("music.playpause", prototype_id="p1"),
            _proto("music.next", embedding=close, prototype_id="p2"),
        ],
        actions=[_descriptor("music.playpause"), _descriptor("music.next")],
    ) is None


def test_direct_rejects_immature_prototype():
    assert resolve_direct_prototype_action(
        embedding=[1.0, 0.0],
        prototypes=[_proto(confirmed=policy.DIRECT_MIN_CONFIRMED - 1)],
        actions=[_descriptor()],
    ) is None


def test_direct_never_dispatches_risky_or_unavailable_actions():
    proto = _proto()
    embedding = [1.0, 0.0]
    # High risk always confirms (§13.1).
    assert resolve_direct_prototype_action(
        embedding=embedding, prototypes=[proto],
        actions=[_descriptor(risk=RISK_HIGH)],
    ) is None
    # Irreversible actions never direct (§8.3).
    assert resolve_direct_prototype_action(
        embedding=embedding, prototypes=[proto],
        actions=[_descriptor(reversible=False)],
    ) is None
    # Unavailable actions never execute from old prototypes (§17).
    assert resolve_direct_prototype_action(
        embedding=embedding, prototypes=[proto],
        actions=[_descriptor(available=False)],
    ) is None


def test_direct_handles_empty_inputs():
    assert resolve_direct_prototype_action(
        embedding=[], prototypes=[_proto()], actions=[_descriptor()]
    ) is None
    assert resolve_direct_prototype_action(
        embedding=[1.0, 0.0], prototypes=[], actions=[_descriptor()]
    ) is None
    assert resolve_direct_prototype_action(
        embedding=[1.0, 0.0], prototypes=[_proto()], actions=[]
    ) is None
