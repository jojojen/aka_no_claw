"""Contract tests for ChatToolPlanner (P1 R1.3a, #74).

Deterministic fake deps prove the planner's own guarantees without a bridge:
- a valid strict-JSON plan resolves to a typed ChatToolPlan + metadata;
- untrusted model output never selects a tool (falls back to plain chat);
- a failing plan backend falls back to plain chat instead of raising;
- backend dispatch routes through the deps seams (bridge monkeypatch points).
"""

from types import SimpleNamespace

from openclaw_adapter.command_bridge_models import (
    CHAT_BACKEND_LOCAL,
    CHAT_TOOL_SEARCH,
    ModelAttempt,
    ModelMetadata,
    parse_request,
)
from openclaw_adapter.command_bridge_planner import ChatToolPlanner


def _metadata() -> ModelMetadata:
    return ModelMetadata(
        requested_provider="local",
        requested_model="m",
        attempted_models=(ModelAttempt("local", "m", "ok"),),
        final_provider="local",
        final_model="m",
    )


class _FakeDeps:
    def __init__(self, raw: object) -> None:
        self._raw = raw
        self.generate_calls: list[tuple[str, str]] = []

    def _build_chat_tool_plan_prompt(self, req, observation=None):
        return f"PROMPT:{req.input}:{observation}"

    def _conversation_key(self, req):
        return "conv-1"

    def _generate_chat_tool_plan_with_chat_backend(
        self, chat_backend, prompt, *, pool_rotation=None, conversation_key=None
    ):
        self.generate_calls.append((chat_backend, prompt))
        if isinstance(self._raw, Exception):
            raise self._raw
        return self._raw, _metadata()

    # dispatch seams
    def _generate_local_chat_tool_plan(self, prompt):
        self.generate_calls.append(("local-seam", prompt))
        return '{"tool":"__no_tool__","answer":"hi","reason_summary":"r"}', _metadata()


def _req(text: str = "東京天氣"):
    return parse_request(
        {"mode": "chat", "input": text, "source": "test", "chat_backend": CHAT_BACKEND_LOCAL}
    )


def _planner(deps) -> ChatToolPlanner:
    return ChatToolPlanner(deps, SimpleNamespace())


def test_select_plan_returns_typed_plan_and_metadata():
    deps = _FakeDeps('{"tool":"/search","query":"東京 天氣","reason_summary":"最新資訊"}')
    plan, metadata = _planner(deps).select_plan(_req())
    assert plan is not None
    assert plan.tool == CHAT_TOOL_SEARCH
    assert plan.query == "東京 天氣"
    assert metadata is not None and metadata.final_provider == "local"
    # The prompt seam and conversation key rode through deps.
    assert deps.generate_calls == [(CHAT_BACKEND_LOCAL, "PROMPT:東京天氣:None")]


def test_select_plan_rejects_untrusted_output():
    deps = _FakeDeps('{"tool":"__import__(\'os\').system","query":"x"}')
    plan, metadata = _planner(deps).select_plan(_req())
    assert plan is None
    assert metadata is None


def test_select_plan_falls_back_when_backend_fails():
    deps = _FakeDeps(RuntimeError("planner down"))
    plan, metadata = _planner(deps).select_plan(_req())
    assert plan is None
    assert metadata is None


def test_generate_with_chat_backend_routes_local_through_deps_seam():
    deps = _FakeDeps("unused")
    text, metadata = _planner(deps).generate_with_chat_backend(
        CHAT_BACKEND_LOCAL, "p"
    )
    assert deps.generate_calls == [("local-seam", "p")]
    assert "__no_tool__" in text
    assert metadata.final_model == "m"
