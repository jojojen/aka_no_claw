from __future__ import annotations

from openclaw_adapter.toolset import build_tool_registry, render_tool_catalog


def test_tool_registry_exposes_assistant_tools() -> None:
    registry = build_tool_registry()
    names = [tool.name for tool in registry.tools()]

    assert "tcg.lookup-card" in names
    assert "tcg.seed-example-watchlist" in names
    assert "market.list-reference-sources" in names
    assert "assistant.serve-dashboard" in names
    assert "assistant.telegram-poll" in names
    assert "assistant.telegram-send-test" in names
    assert "lookup-card" in registry.tools()[0].aliases


def test_tool_catalog_mentions_lookup_tool() -> None:
    registry = build_tool_registry()
    catalog = render_tool_catalog(registry)

    assert "OpenClaw assistant tools:" in catalog
    assert "tcg.lookup-card" in catalog
    assert "market.list-reference-sources" in catalog
    assert "assistant.serve-dashboard" in catalog
    assert "assistant.telegram-poll" in catalog
