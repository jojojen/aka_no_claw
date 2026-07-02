"""Tests for shared chat-goal/workflow drafting helpers (#54 phase 2)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from openclaw_adapter.goal_planner import (
    GoalPlanner,
    build_goal_workflow_prompt,
    extract_json_object,
    generate_workflow_from_goal,
)


class _FakeLLM:
    def __init__(self, response):
        self.response = response
        self.prompts = []

    def generate(self, prompt, *, temperature=0.0):
        self.prompts.append(prompt)
        if isinstance(self.response, list):
            return self.response.pop(0)
        return self.response


class _DroppingLLM:
    def __init__(self):
        self.prompts = []

    def generate(self, prompt, *, temperature=0.0):
        self.prompts.append(prompt)
        raise ConnectionError("Remote end closed connection without response")


def test_extract_json_object_with_prose_and_fence():
    text = '好的：\n```json\n{"id":"wf-x","goal":"test","steps":[]}\n```'
    assert extract_json_object(text) == {"id": "wf-x", "goal": "test", "steps": []}


def test_build_goal_workflow_prompt_includes_registered_command_usage():
    registry = {
        "/music": SimpleNamespace(usage="playbest=播放最愛；stop=停止"),
        "/saynow": SimpleNamespace(usage="把文字念出來"),
        "/workflow": SimpleNamespace(usage="should be filtered"),
    }

    prompt = build_goal_workflow_prompt(
        "先播放最愛再播報",
        catalog=None,
        command_registry=registry,
    )

    assert "/music" in prompt
    assert "playbest=播放最愛" in prompt
    assert "/saynow" in prompt
    assert "/workflow" not in prompt


def test_generate_workflow_from_goal_uses_fallback_and_backfills():
    fallback = _FakeLLM(json.dumps({"steps": []}, ensure_ascii=False))
    wf, err, used_fallback = generate_workflow_from_goal(
        "我的描述",
        _DroppingLLM(),
        catalog=None,
        fallback_client=fallback,
    )
    assert err is None
    assert used_fallback is True
    assert wf.id == "wf-draft"
    assert wf.goal == "我的描述"
    assert fallback.prompts and "我的描述" in fallback.prompts[0]


def test_goal_planner_draft_returns_valid_workflow():
    llm = _FakeLLM(json.dumps({
        "id": "wf-morning",
        "goal": "先播放最愛再播報",
        "steps": [
            {"id": "s1", "kind": "command_sink", "command": "/music", "literal": "playbest", "output": "music"},
            {"id": "s2", "kind": "command_sink", "command": "/saynow", "literal": "早安", "output": "spoken"},
        ],
    }, ensure_ascii=False))
    planner = GoalPlanner(
        catalog=None,
        llm_client=llm,
        command_registry={
            "/music": SimpleNamespace(usage="playbest=播放最愛"),
            "/saynow": SimpleNamespace(usage="把文字念出來"),
        },
    )
    wf, err, used_fallback = planner.draft("先播放最愛再播報")
    assert err is None
    assert used_fallback is False
    assert wf.validate_references(known_commands=frozenset({"/music", "/saynow"})) == []


def test_goal_planner_pool_rotation_advances_start_provider_across_calls():
    """A shared CloudPoolRotation on GoalPlanner rotates which client in the
    list is tried first on each draft/replan call, instead of always retrying
    client[0] first — so one long goal spreads load across the cloud pool.
    """
    from openclaw_adapter.llm_pool_settings import CloudPoolRotation

    good_wf = json.dumps({"id": "wf-x", "goal": "g", "steps": []}, ensure_ascii=False)
    first = _FakeLLM([good_wf, good_wf])
    second = _FakeLLM([good_wf, good_wf])
    planner = GoalPlanner(
        catalog=None,
        llm_client=[first, second],
        pool_rotation=CloudPoolRotation(),
    )

    planner.draft("g")
    assert len(first.prompts) == 1 and len(second.prompts) == 0

    trace = SimpleNamespace(to_dict=lambda: {})
    workflow = SimpleNamespace(to_dict=lambda: {})
    planner.replan("g", workflow, trace)
    assert len(first.prompts) == 1 and len(second.prompts) == 1


def test_goal_planner_without_pool_rotation_always_tries_first_client():
    good_wf = json.dumps({"id": "wf-x", "goal": "g", "steps": []}, ensure_ascii=False)
    first = _FakeLLM([good_wf, good_wf])
    second = _FakeLLM([good_wf, good_wf])
    planner = GoalPlanner(catalog=None, llm_client=[first, second])

    planner.draft("g")
    trace = SimpleNamespace(to_dict=lambda: {})
    workflow = SimpleNamespace(to_dict=lambda: {})
    planner.replan("g", workflow, trace)

    assert len(first.prompts) == 2 and len(second.prompts) == 0


def test_generate_workflow_from_goal_repairs_invalid_references_once():
    broken = json.dumps({
        "id": "wf-broken",
        "goal": "先播報再念",
        "steps": [
            {"id": "s1", "kind": "command_sink", "command": "/saynow", "input": "greeting", "output": "spoken"},
        ],
    }, ensure_ascii=False)
    fixed = json.dumps({
        "id": "wf-fixed",
        "goal": "先播報再念",
        "steps": [
            {"id": "s1", "kind": "llm_transform", "inputs": [], "instructions": "產生早安問候", "output": "greeting"},
            {"id": "s2", "kind": "command_sink", "command": "/saynow", "input": "greeting", "output": "spoken"},
        ],
    }, ensure_ascii=False)
    llm = _FakeLLM([broken, fixed])
    wf, err, used_fallback = generate_workflow_from_goal(
        "先播報再念",
        llm,
        catalog=None,
        command_registry={"/saynow": SimpleNamespace(usage="把文字念出來")},
    )
    assert err is None
    assert used_fallback is False
    assert wf.id == "wf-fixed"
    assert len(llm.prompts) == 2
    assert "驗證錯誤" in llm.prompts[1]
    assert "greeting" in llm.prompts[1]


def test_generate_workflow_from_goal_refuses_invalid_draft_after_one_repair():
    bad = json.dumps({
        "id": "wf-bad",
        "goal": "壞草稿",
        "steps": [
            {"id": "s1", "kind": "command_sink", "command": "/missing", "literal": "x", "output": "r1"},
        ],
    }, ensure_ascii=False)
    llm = _FakeLLM([bad, bad])
    wf, err, _ = generate_workflow_from_goal(
        "壞草稿",
        llm,
        catalog=None,
        command_registry={"/saynow": SimpleNamespace(usage="把文字念出來")},
    )
    assert wf is None
    assert "工作流草稿驗證失敗" in err
    assert "not registered" in err
    assert len(llm.prompts) == 2


def test_generate_workflow_from_goal_tries_next_client_after_invalid_repair():
    bad = json.dumps({
        "id": "wf-bad",
        "goal": "壞草稿",
        "steps": [
            {"id": "s1", "kind": "command_sink", "command": "/missing", "literal": "x", "output": "r1"},
        ],
    }, ensure_ascii=False)
    good = json.dumps({
        "id": "wf-good",
        "goal": "播放熱門歌曲",
        "steps": [
            {"id": "s1", "kind": "command_sink", "command": "/search", "literal": "熱門歌曲", "output": "search"},
            {"id": "s2", "kind": "command_sink", "command": "/musiclistall", "literal": "", "output": "local"},
            {"id": "s3", "kind": "llm_transform", "inputs": ["search", "local"], "instructions": "比對並輸出歌曲名", "output": "song"},
            {"id": "s4", "kind": "command_sink", "command": "/music", "input": "song", "output": "played"},
        ],
    }, ensure_ascii=False)
    first = _FakeLLM([bad, bad])
    second = _FakeLLM(good)

    wf, err, used_fallback = generate_workflow_from_goal(
        "播放熱門歌曲",
        [first, second],
        catalog=None,
        command_registry={
            "/search": SimpleNamespace(usage="搜尋"),
            "/musiclistall": SimpleNamespace(usage="列出音樂"),
            "/music": SimpleNamespace(usage="播放音樂"),
        },
    )

    assert err is None
    assert used_fallback is True
    assert wf.id == "wf-good"
    assert len(first.prompts) == 2
    assert len(second.prompts) == 1


def test_generate_workflow_from_goal_tolerates_invalid_draft_when_not_strict():
    bad = json.dumps({
        "id": "wf-bad",
        "goal": "壞草稿",
        "steps": [
            {"id": "s1", "kind": "command_sink", "command": "/missing", "literal": "x", "output": "r1"},
        ],
    }, ensure_ascii=False)
    llm = _FakeLLM([bad, bad])
    wf, err, _ = generate_workflow_from_goal(
        "壞草稿",
        llm,
        catalog=None,
        command_registry={"/saynow": SimpleNamespace(usage="把文字念出來")},
        strict=False,
    )
    assert wf is not None
    assert wf.id == "wf-bad"
    assert "工作流草稿驗證失敗" in err
    assert "not registered" in err


def test_generate_workflow_from_goal_refuses_denylisted_sink():
    bad = json.dumps({
        "id": "wf-restart",
        "goal": "重啟",
        "steps": [
            {"id": "s1", "kind": "command_sink", "command": "/restartall", "literal": "", "output": "r1"},
        ],
    }, ensure_ascii=False)
    llm = _FakeLLM([bad, bad])
    wf, err, _ = generate_workflow_from_goal(
        "重啟",
        llm,
        catalog=None,
        command_registry={"/restartall": SimpleNamespace(usage="restart")},
    )
    assert wf is None
    assert "not allowed" in err


def test_generate_workflow_from_goal_refuses_unknown_tool_slug():
    bad = json.dumps({
        "id": "wf-tool",
        "goal": "工具",
        "steps": [
            {"id": "s1", "kind": "tool_call", "tool": "made_up_tool", "args": {}, "output": "r1"},
        ],
    }, ensure_ascii=False)
    llm = _FakeLLM([bad, bad])

    class _Catalog:
        def entries(self):
            return [SimpleNamespace(slug="real_tool", description="real")]

    wf, err, _ = generate_workflow_from_goal("工具", llm, _Catalog())
    assert wf is None
    assert "does not exist in the generated-tool catalog" in err
