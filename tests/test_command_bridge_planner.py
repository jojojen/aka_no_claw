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


class _PromptDeps:
    """Deps for build_plan_prompt: real prompt assembly, stubbed seams."""

    def __init__(self, ledger: list[dict] | None = None) -> None:
        self._ledger = ledger or []

    def _chat_tool_plan_system_prompt(self):
        return "SYSTEM"

    def _chat_tool_ledger_entries(self, req):
        return self._ledger


def test_plan_prompt_forbids_state_gating_of_requested_actions():
    # Ledger history once claimed "nothing playing" while a goal-started track
    # was audible, and the planner refused a stop request. Requested actions
    # must be dispatched as asked — the tool reports reality, not the ledger.
    prompt = _planner(_PromptDeps()).build_plan_prompt(_req("音樂停止"))
    assert "以使用者的要求為準" in prompt
    assert "不要根據對話紀錄或工具紀錄推測" in prompt
    assert "使用者最新訊息：音樂停止" in prompt


def test_plan_prompt_does_not_inject_out_of_band_ledger():
    ledger = [
        {"tool": "/music", "query": "resume", "status": "partial",
         "summary": "工具回覆表示無可繼續播放的音樂"},
    ]
    prompt = _planner(_PromptDeps(ledger)).build_plan_prompt(_req("音樂停止"))
    assert "無可繼續播放的音樂" not in prompt
    assert "使用者最新訊息：音樂停止" in prompt
