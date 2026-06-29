"""Phase 1 of #52: catalog/lifecycle view over generated_tools/manifest.json.

Deterministic — no live weather, no codegen client, no web search. The catalog
only reads the manifest and a sidecar catalog.json; these tests drive it with
hand-written manifest fixtures and assert the derived lifecycle status and
metric bookkeeping.
"""

import json

import pytest

from openclaw_adapter.generated_tool_catalog import (
    GeneratedToolCatalog,
    STATUS_BLOCKED,
    STATUS_CANDIDATE,
    STATUS_DEMOTED,
    STATUS_INELIGIBLE,
    STATUS_PROMOTED,
    STATUS_RECOVERING,
)


def _write_manifest(tools_dir, entries):
    (tools_dir / "manifest.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _weather_entry(slug="tool_ca00010a"):
    return {
        "id": "1fbf107a93a3",
        "slug": slug,
        "request": "現在日本東京的氣溫跟濕度",
        "description": "查詢城市天氣",
        "requires": [],
        "created_at": "2026-06-01T01:42:35+00:00",
        "path": f"{slug}/tool.py",
        "param_schema": [{"name": "city", "type": "string", "desc": "城市名"}],
        "tool_type": "城市天氣查詢",
    }


@pytest.fixture
def catalog(tmp_path):
    return GeneratedToolCatalog(tmp_path)


# A. First-use generation becomes candidate -------------------------------------

def test_validated_parameterized_tool_is_candidate(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry()])
    cands = catalog.candidates()
    assert [c.slug for c in cands] == ["tool_ca00010a"]
    c = cands[0]
    assert c.status == STATUS_CANDIDATE
    assert c.tool_type == "城市天氣查詢"
    assert c.param_schema[0]["name"] == "city"
    assert c.source == "generated_tools/tool_ca00010a/tool.py"
    assert c.safety_profile["writes"] == "generated_tools_only"


def test_missing_manifest_yields_empty_catalog(catalog):
    assert catalog.entries() == []
    assert catalog.candidates() == []


def test_planner_view_hides_metrics_and_marks_reuse_execution(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry()])
    view = catalog.candidates()[0].planner_view()
    assert view["name"] == "generated.tool_ca00010a"
    assert view["execution"] == "dynamic_tool_runner_reuse"
    assert "metrics" not in view  # planner never sees raw counters


# D. Do not promote one-off/static tools ----------------------------------------

def test_static_one_off_without_schema_is_ineligible(tmp_path, catalog):
    entry = _weather_entry(slug="static_x")
    entry.pop("param_schema")
    entry.pop("tool_type")
    _write_manifest(tmp_path, [entry])
    e = catalog.get("static_x")
    assert e.status == STATUS_INELIGIBLE
    assert catalog.candidates() == []
    assert catalog.promoted() == []


def test_empty_schema_is_not_a_candidate(tmp_path, catalog):
    entry = _weather_entry(slug="emptyschema")
    entry["param_schema"] = []
    _write_manifest(tmp_path, [entry])
    assert catalog.get("emptyschema").status == STATUS_INELIGIBLE


# C. Promotion after reliable reuse ---------------------------------------------

def test_candidate_promotes_after_one_successful_reuse(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry()])
    assert catalog.get("tool_ca00010a").status == STATUS_CANDIDATE
    updated = catalog.record_reuse_success("tool_ca00010a")
    assert updated.status == STATUS_PROMOTED
    assert "successful_reuse" in updated.promotion_reason
    assert catalog.get("tool_ca00010a").status == STATUS_PROMOTED
    assert [p.slug for p in catalog.promoted()] == ["tool_ca00010a"]


def test_reuse_metrics_persist_across_catalog_instances(tmp_path):
    _write_manifest(tmp_path, [_weather_entry()])
    GeneratedToolCatalog(tmp_path).record_reuse_success("tool_ca00010a")
    fresh = GeneratedToolCatalog(tmp_path)
    e = fresh.get("tool_ca00010a")
    assert e.status == STATUS_PROMOTED
    assert e.metrics["reuse_success_count"] == 1
    assert e.metrics["last_success_at"] is not None


def test_manual_approval_only_mode_needs_explicit_approve(tmp_path):
    _write_manifest(tmp_path, [_weather_entry()])
    cat = GeneratedToolCatalog(tmp_path, require_manual_approval=True)
    cat.record_reuse_success("tool_ca00010a")
    # reuse alone does not promote when manual approval is required
    assert cat.get("tool_ca00010a").status == STATUS_CANDIDATE
    cat.approve("tool_ca00010a")
    assert cat.get("tool_ca00010a").status == STATUS_PROMOTED


# E. Demotion after repeated failures -------------------------------------------

def test_promoted_tool_demotes_after_threshold_failures(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry()])
    catalog.record_reuse_success("tool_ca00010a")
    assert catalog.get("tool_ca00010a").status == STATUS_PROMOTED
    for _ in range(3):
        catalog.record_failure("tool_ca00010a", reason="bad output")
    e = catalog.get("tool_ca00010a")
    assert e.status == STATUS_DEMOTED
    assert e.metrics["consecutive_failures"] == 3
    assert e.metrics["last_failure_reason"] == "bad output"
    assert catalog.promoted() == []


def test_success_resets_consecutive_failures(tmp_path, catalog):
    # A success clears the consecutive-failure counter, but a tool with prior
    # failures does NOT jump straight back to promoted (Phase 3 cautious
    # recovery): one clean reuse leaves it RECOVERING, still reusable.
    _write_manifest(tmp_path, [_weather_entry()])
    catalog.record_failure("tool_ca00010a")
    catalog.record_failure("tool_ca00010a")
    catalog.record_reuse_success("tool_ca00010a")
    e = catalog.get("tool_ca00010a")
    assert e.metrics["consecutive_failures"] == 0
    assert e.metrics["failure_count"] == 2
    assert e.metrics["clean_streak"] == 1
    assert e.status == STATUS_RECOVERING


def test_demoted_then_recovers_cautiously_over_two_clean_reuses(tmp_path, catalog):
    # A demoted tool earns trust back gradually: first clean reuse → recovering
    # (still reusable so it can keep earning), second clean reuse → promoted.
    _write_manifest(tmp_path, [_weather_entry()])
    for _ in range(3):
        catalog.record_failure("tool_ca00010a")
    assert catalog.get("tool_ca00010a").status == STATUS_DEMOTED

    catalog.record_reuse_success("tool_ca00010a")
    e1 = catalog.get("tool_ca00010a")
    assert e1.status == STATUS_RECOVERING
    assert "1/2" in e1.promotion_reason
    # a recovering tool stays in the reuse pool so it can keep earning trust
    assert "tool_ca00010a" in [r.slug for r in catalog.reusable()]
    assert "tool_ca00010a" not in catalog.reuse_suppressed()

    catalog.record_reuse_success("tool_ca00010a")
    e2 = catalog.get("tool_ca00010a")
    assert e2.status == STATUS_PROMOTED
    assert "recovered" in e2.promotion_reason


def test_recovery_resets_if_it_fails_again(tmp_path, catalog):
    # A flaky tool that fails mid-recovery loses its clean streak and must start
    # the recovery climb over — this is the oscillation guard Phase 3 adds.
    _write_manifest(tmp_path, [_weather_entry()])
    for _ in range(3):
        catalog.record_failure("tool_ca00010a")
    catalog.record_reuse_success("tool_ca00010a")  # clean_streak 1, recovering
    catalog.record_failure("tool_ca00010a")        # streak reset to 0
    e = catalog.get("tool_ca00010a")
    assert e.metrics["clean_streak"] == 0
    catalog.record_reuse_success("tool_ca00010a")  # back to 1, still recovering
    assert catalog.get("tool_ca00010a").status == STATUS_RECOVERING


def test_manual_approval_skips_recovery_tax(tmp_path, catalog):
    # An operator vouching for a tool overrides cautious recovery: one clean
    # reuse after approval promotes immediately even with prior failures.
    _write_manifest(tmp_path, [_weather_entry()])
    catalog.record_failure("tool_ca00010a")
    catalog.record_failure("tool_ca00010a")
    catalog.approve("tool_ca00010a")
    catalog.record_reuse_success("tool_ca00010a")
    assert catalog.get("tool_ca00010a").status == STATUS_PROMOTED


def test_clean_first_timer_still_promotes_in_one_reuse(tmp_path, catalog):
    # Criterion C preserved: a tool that has NEVER failed promotes after a
    # single reuse — the recovery tax only applies to previously-broken tools.
    _write_manifest(tmp_path, [_weather_entry()])
    catalog.record_reuse_success("tool_ca00010a")
    assert catalog.get("tool_ca00010a").status == STATUS_PROMOTED


# H. Safety guardrails ----------------------------------------------------------

def test_path_escaping_generated_tools_is_blocked(tmp_path, catalog):
    entry = _weather_entry(slug="escapee")
    entry["path"] = "../../etc/passwd"
    _write_manifest(tmp_path, [entry])
    e = catalog.get("escapee")
    assert e.status == STATUS_BLOCKED


def test_absolute_path_is_blocked(tmp_path, catalog):
    entry = _weather_entry(slug="abs")
    entry["path"] = "/tmp/evil/tool.py"
    _write_manifest(tmp_path, [entry])
    assert catalog.get("abs").status == STATUS_BLOCKED


def test_unapproved_dependency_blocks_promotion(tmp_path):
    entry = _weather_entry(slug="needs_pkg")
    entry["requires"] = ["requests"]
    _write_manifest(tmp_path, [entry])
    # default: no approved deps -> blocked
    assert GeneratedToolCatalog(tmp_path).get("needs_pkg").status == STATUS_BLOCKED
    # approving the dep lets it be a candidate again
    cat = GeneratedToolCatalog(tmp_path, approved_requires=["requests"])
    assert cat.get("needs_pkg").status == STATUS_CANDIDATE


def test_manual_block_overrides_promotion(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry()])
    catalog.record_reuse_success("tool_ca00010a")
    catalog.block("tool_ca00010a", reason="safety violation")
    e = catalog.get("tool_ca00010a")
    assert e.status == STATUS_BLOCKED
    assert e.promotion_reason == "safety violation"
    # unblock restores it to the reuse-earned promotion
    catalog.unblock("tool_ca00010a")
    assert catalog.get("tool_ca00010a").status in (STATUS_CANDIDATE, STATUS_PROMOTED)


def test_forged_status_in_sidecar_is_ignored(tmp_path, catalog):
    # A tampered catalog.json claiming "promoted" must not bypass classification:
    # status is always recomputed from metrics, and an unapproved dep stays blocked.
    entry = _weather_entry(slug="forge")
    entry["requires"] = ["evilpkg"]
    _write_manifest(tmp_path, [entry])
    (tmp_path / "catalog.json").write_text(
        json.dumps({"forge": {"status": "promoted", "reuse_success_count": 99}}),
        encoding="utf-8",
    )
    assert catalog.get("forge").status == STATUS_BLOCKED


def test_mutating_unknown_slug_returns_none(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry()])
    assert catalog.record_reuse_success("does_not_exist") is None
    # no spurious state row written
    assert not (tmp_path / "catalog.json").exists() or \
        "does_not_exist" not in json.loads((tmp_path / "catalog.json").read_text())


# Phase 1.1 — malformed sidecar hardening (review blocker) -----------------------

def _write_sidecar(tmp_path, payload):
    (tmp_path / "catalog.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_string_numbers_in_sidecar_do_not_crash_classification(tmp_path, catalog):
    # A tampered/corrupted sidecar with string counts must not raise TypeError
    # during the int >= int comparisons in _classify (catalog-read DoS).
    _write_manifest(tmp_path, [_weather_entry()])
    _write_sidecar(tmp_path, {"tool_ca00010a": {
        "reuse_success_count": "999",
        "consecutive_failures": "0",
    }})
    e = catalog.get("tool_ca00010a")  # must not raise
    assert e.metrics["reuse_success_count"] == 999
    assert e.metrics["consecutive_failures"] == 0
    assert e.status == STATUS_PROMOTED


def test_non_numeric_garbage_clamps_to_safe_defaults(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry()])
    _write_sidecar(tmp_path, {"tool_ca00010a": {
        "reuse_success_count": "not-a-number",
        "consecutive_failures": None,
        "failure_count": [1, 2, 3],
        "generation_success_count": {},
    }})
    e = catalog.get("tool_ca00010a")
    assert e.metrics["reuse_success_count"] == 0
    assert e.metrics["consecutive_failures"] == 0
    assert e.metrics["failure_count"] == 0
    assert e.metrics["generation_success_count"] == 1  # default
    assert e.status == STATUS_CANDIDATE


def test_negative_counts_clamp_to_default(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry()])
    _write_sidecar(tmp_path, {"tool_ca00010a": {"consecutive_failures": -5}})
    e = catalog.get("tool_ca00010a")
    assert e.metrics["consecutive_failures"] == 0
    assert e.status == STATUS_CANDIDATE


def test_non_bool_blocked_flag_is_not_truthy(tmp_path, catalog):
    # A string "true" must NOT be honored as a real boolean (strict coercion).
    _write_manifest(tmp_path, [_weather_entry()])
    _write_sidecar(tmp_path, {"tool_ca00010a": {
        "manual_approved": "true",
        "blocked": 1,
    }})
    e = catalog.get("tool_ca00010a")
    assert e.metrics["manual_approved"] is False
    assert e.metrics["blocked"] is False
    # neither forged flag promoted or blocked it
    assert e.status == STATUS_CANDIDATE


def test_real_json_bool_blocked_is_honored(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry()])
    _write_sidecar(tmp_path, {"tool_ca00010a": {"blocked": True,
                                                "blocked_reason": "safety"}})
    assert catalog.get("tool_ca00010a").status == STATUS_BLOCKED


def test_non_string_reason_is_coerced(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry()])
    _write_sidecar(tmp_path, {"tool_ca00010a": {
        "consecutive_failures": 3,
        "last_failure_reason": 12345,
    }})
    e = catalog.get("tool_ca00010a")
    assert e.metrics["last_failure_reason"] == "12345"
    assert e.status == STATUS_DEMOTED


def test_non_dict_state_row_is_ignored(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry()])
    _write_sidecar(tmp_path, {"tool_ca00010a": "garbage"})
    assert catalog.get("tool_ca00010a").status == STATUS_CANDIDATE


def test_corrupt_sidecar_json_is_ignored(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry()])
    (tmp_path / "catalog.json").write_text("{not valid json", encoding="utf-8")
    assert catalog.get("tool_ca00010a").status == STATUS_CANDIDATE


def test_reuse_suppressed_survives_malformed_sidecar(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry()])
    _write_sidecar(tmp_path, {"tool_ca00010a": {"consecutive_failures": "3"}})
    assert catalog.reuse_suppressed() == {"tool_ca00010a"}


def test_reusable_excludes_blocked_and_ineligible(tmp_path, catalog):
    good = _weather_entry(slug="good")
    static = _weather_entry(slug="static")
    static.pop("param_schema")
    static.pop("tool_type")
    blocked = _weather_entry(slug="blocked")
    blocked["path"] = "/abs/tool.py"
    _write_manifest(tmp_path, [good, static, blocked])
    assert sorted(e.slug for e in catalog.reusable()) == ["good"]


# G. Top-k lexical retrieval (Phase 4) ------------------------------------------

def _stock_entry(slug="tool_stock01"):
    return {
        "id": "deadbeef",
        "slug": slug,
        "request": "查詢台積電股價",
        "description": "查詢股票即時報價",
        "requires": [],
        "created_at": "2026-06-02T00:00:00+00:00",
        "path": f"{slug}/tool.py",
        "param_schema": [{"name": "symbol", "type": "string", "desc": "股票代號"}],
        "tool_type": "股票報價查詢",
    }


def test_retrieve_ranks_relevant_tool_first(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry(), _stock_entry()])
    hits = catalog.retrieve("查大阪天氣")
    assert hits, "expected at least one retrieval hit"
    assert hits[0].slug == "tool_ca00010a"  # weather tool, not the stock tool
    assert hits[0].tool_type == "城市天氣查詢"


def test_retrieve_disjoint_query_returns_nothing(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry(), _stock_entry()])
    # No lexical overlap with either tool's fields.
    assert catalog.retrieve("xyzzy plugh foobar") == []


def test_retrieve_empty_query_returns_empty(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry()])
    assert catalog.retrieve("") == []
    assert catalog.retrieve("   ") == []


def test_retrieve_respects_top_k(tmp_path, catalog):
    entries = [_weather_entry(slug=f"w{i}") for i in range(5)]
    _write_manifest(tmp_path, entries)
    assert len(catalog.retrieve("查天氣", k=2)) == 2


def test_retrieve_excludes_blocked_demoted_ineligible(tmp_path, catalog):
    good = _weather_entry(slug="good")
    blocked = _weather_entry(slug="blocked")
    blocked["path"] = "/abs/tool.py"  # path escape -> blocked
    static = _weather_entry(slug="static")  # ineligible (no schema)
    static.pop("param_schema")
    static.pop("tool_type")
    demoted = _weather_entry(slug="demoted")
    _write_manifest(tmp_path, [good, blocked, static, demoted])
    for _ in range(3):
        catalog.record_failure("demoted")
    hits = {e.slug for e in catalog.retrieve("查天氣")}
    assert hits == {"good"}


def test_retrieve_promoted_only_filters_candidates(tmp_path, catalog):
    promoted = _weather_entry(slug="promoted")
    candidate = _weather_entry(slug="candidate")
    _write_manifest(tmp_path, [promoted, candidate])
    catalog.record_reuse_success("promoted")  # clean first-timer -> promoted
    assert catalog.get("promoted").status == STATUS_PROMOTED
    assert catalog.get("candidate").status == STATUS_CANDIDATE
    hits = {e.slug for e in catalog.retrieve("查天氣", promoted_only=True)}
    assert hits == {"promoted"}


def test_planner_tools_returns_schema_without_metrics(tmp_path, catalog):
    _write_manifest(tmp_path, [_weather_entry()])
    views = catalog.planner_tools("查大阪天氣")
    assert views[0]["name"] == "generated.tool_ca00010a"
    assert views[0]["execution"] == "dynamic_tool_runner_reuse"
    assert "metrics" not in views[0]
    assert "example_request" not in views[0]  # planner surface stays minimal
