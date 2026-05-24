"""Unit tests for /knowledge Telegram command dispatcher."""

from __future__ import annotations

import pytest

from assistant_runtime import AssistantSettings
from openclaw_adapter.knowledge_command import build_knowledge_handler
from openclaw_adapter.knowledge_db import KnowledgeDatabase


@pytest.fixture
def handler(tmp_path):
    settings = AssistantSettings(knowledge_db_path=str(tmp_path / "kb.sqlite3"))
    return build_knowledge_handler(settings)


def test_empty_command_shows_usage(handler):
    out = handler("", "1")
    assert "用法" in out
    assert "/knowledge add" in out


def test_add_requires_pipe_separator(handler):
    out = handler("add pjsk just a summary without separator", "1")
    assert "`|`" in out or "|" in out


def test_add_writes_entry_with_manual_origin(handler, tmp_path):
    out = handler("add pjsk | プロセカ。SEGA + Crypton。", "1")
    assert "✅" in out
    db = KnowledgeDatabase(tmp_path / "kb.sqlite3")
    entry = db.get_entry("pjsk")
    assert entry is not None
    assert entry.origin == "manual"
    assert entry.confidence == 1.0
    assert "プロセカ" in entry.summary


def test_add_with_explicit_type(handler, tmp_path):
    out = handler("add アビスアイ as set | ポケカ拡張パック", "1")
    assert "type=set" in out
    db = KnowledgeDatabase(tmp_path / "kb.sqlite3")
    entry = db.get_entry("アビスアイ")
    assert entry.entity_type == "set"


def test_add_with_invalid_type_returns_error(handler):
    out = handler("add x as nonsense | summary text here", "1")
    assert "未知" in out or "合法值" in out


def test_list_shows_recent_entries(handler):
    handler("add a | text-a", "1")
    handler("add b | text-b", "1")
    out = handler("list", "1")
    assert "a" in out and "b" in out


def test_get_returns_full_entry(handler):
    handler("add pjsk | プロセカ詳細描述", "1")
    out = handler("get pjsk", "1")
    assert "pjsk" in out
    assert "プロセカ詳細描述" in out


def test_get_via_alias_after_alias_command(handler):
    handler("add pjsk | プロセカ summary", "1")
    handler("alias pjsk = プロセカ, Project Sekai", "1")
    out = handler("get プロセカ", "1")
    assert "pjsk" in out


def test_get_missing_returns_message(handler):
    out = handler("get not_in_db", "1")
    assert "找不到" in out


def test_alias_requires_existing_entity(handler):
    out = handler("alias missing = a, b", "1")
    assert "尚未在知識庫" in out


def test_remove_deletes_entry(handler):
    handler("add to_delete | content", "1")
    out = handler("remove to_delete", "1")
    assert "✅" in out
    out2 = handler("get to_delete", "1")
    assert "找不到" in out2


def test_remove_missing_returns_message(handler):
    out = handler("remove nothing_here", "1")
    assert "找不到" in out
