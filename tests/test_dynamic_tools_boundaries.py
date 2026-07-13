"""R4.0 module-boundary characterization for the dynamic-tool pipeline.

Pins the two things the R4.1-R4.8 extraction (splitting ``dynamic_tools.py`` into
a ``dynamic_tools/`` package of separated collaborators) must NOT break:

1. the public import surface every ``src/`` consumer and test depends on, and
2. the separation-of-concerns invariants from the plan (§12): safety admissibility
   is generator-independent, and the safety/spec helpers are module-level free
   functions rather than runner-bound state.

Behavioral contracts (repair budget, grounding, validator gating, secret
stripping) are already pinned by ``tests/test_dynamic_tools.py``; this file adds
only the structural boundary pins. See ``docs/R4_DYNAMIC_TOOLS_INVENTORY.md``.
"""

from __future__ import annotations

import importlib

import pytest

# Names imported from ``openclaw_adapter.dynamic_tools`` by other src/ modules.
# Breaking any of these breaks a real consumer (command_bridge, telegram_bot,
# fix_command, natural_language, goal_planner, sns_tools, research_telegram).
SRC_CONSUMED = (
    "build_dynamic_tool_runner_from_settings",
    "DynamicToolRunner",
    "OllamaTextClient",
    "_resolve_tools_dir",
    "OpenCodeTextClient",
    "probe_opencode",
    "_build_mistral_client",
    "MistralTextClient",
    "NvidiaTextClient",
    "build_research_cloud_text_client",
    "CloudBackendUnavailable",
    "_extract_code",
)

# Additional names imported by the test suite.
TEST_CONSUMED = (
    "DynamicToolResult",
    "ReusePlan",
    "SearchGroundingBudgetExhausted",
    "OpenCodeCliTextClient",
    "_extract_answer",
    "_check_numeric",
    "_check_direction",
    "_syntax_error",
    "_is_truncation_error",
    "_defaults_schema_from_code",
    "_ensure_stdlib_imports",
    "probe_ollama",
)

PUBLIC_SURFACE = SRC_CONSUMED + TEST_CONSUMED


@pytest.fixture(scope="module")
def dynamic_tools():
    return importlib.import_module("openclaw_adapter.dynamic_tools")


@pytest.mark.parametrize("name", PUBLIC_SURFACE)
def test_public_surface_is_importable(dynamic_tools, name):
    assert hasattr(dynamic_tools, name), (
        f"{name} must stay importable from openclaw_adapter.dynamic_tools; "
        "a consumer depends on it (see docs/R4_DYNAMIC_TOOLS_INVENTORY.md §2)."
    )


def test_safety_admissibility_is_generator_independent(dynamic_tools):
    """Package allow/deny decides from the name alone — no runner, no model.

    This is the plan §12 trust boundary: a generated tool cannot widen its own
    allowlist, because admissibility is a pure module-level predicate.
    """
    is_safe = dynamic_tools._is_safe_pkg
    is_approved = dynamic_tools._is_approved_pkg
    # Called with only a package name (no self / runner / provider context).
    assert is_safe("requests") is True
    assert is_safe("os; rm -rf /") is False
    assert is_safe("../evil") is False
    # Approval is a strict subset gate and equally context-free.
    assert isinstance(is_approved("requests"), bool)


def test_safety_and_spec_helpers_are_free_functions(dynamic_tools):
    """The helpers R4 splits out are module-level, not DynamicToolRunner methods.

    Guarantees the extraction can move them into ``safety.py`` / ``specification.py``
    without untangling them from runner instance state first.
    """
    import inspect

    for name in (
        "_is_safe_pkg",
        "_is_approved_pkg",
        "_syntax_error",
        "_ensure_stdlib_imports",
        "_extract_code",
        "_extract_answer",
        "_normalize_request",
    ):
        obj = getattr(dynamic_tools, name)
        assert inspect.isfunction(obj), f"{name} should be a module-level function"
        # Not bound to DynamicToolRunner — first param is not ``self``.
        params = list(inspect.signature(obj).parameters)
        assert not params or params[0] != "self", f"{name} must not be a method"


def test_syntax_error_reads_code_text_only(dynamic_tools):
    """The syntax gate classifies from code text alone (generator-independent)."""
    assert dynamic_tools._syntax_error("def f(:\n    pass\n")  # broken → message
    assert dynamic_tools._syntax_error("x = 1\n") == ""       # valid → empty
