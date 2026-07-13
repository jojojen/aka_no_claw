from __future__ import annotations

from types import SimpleNamespace

from openclaw_adapter import command_bridge_providers, local_text


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        openclaw_local_text_backend="ollama",
        openclaw_local_text_endpoint="http://127.0.0.1:11434",
        openclaw_local_text_model="qwen3:14b",
        openclaw_local_text_timeout_seconds=30,
    )


def test_translate_handler_defaults_to_cloud_pool(monkeypatch) -> None:
    """Translation must default to the cloud pool (matching the web console),
    not local qwen3:14b. The local path must NOT be touched on cloud success."""
    seen: dict[str, object] = {}

    def _fake_cloud(settings, prompt):
        seen["prompt"] = prompt
        return "  這是一支筆。  "

    monkeypatch.setattr(command_bridge_providers, "generate_via_cloud_pool", _fake_cloud)

    def _boom(**kwargs):
        raise AssertionError("local model must not be called when cloud succeeds")

    monkeypatch.setattr(local_text, "_call_local_text_model", _boom)

    handler = local_text.build_translate_handler(_settings(), target="zh")
    assert handler("これはペンです", "chat-1") == "這是一支筆。"
    assert "繁體中文" in str(seen["prompt"])


def test_translate_handler_falls_back_to_local_when_cloud_fails(monkeypatch) -> None:
    """When the cloud pool raises (all cloud down AND local disabled in-pool, or
    a mid-flight blowup), the handler must still deliver via a local call rather
    than silently dropping the translation."""
    def _boom_cloud(settings, prompt):
        raise RuntimeError("雲端池目前沒有可用模型。")

    monkeypatch.setattr(command_bridge_providers, "generate_via_cloud_pool", _boom_cloud)
    monkeypatch.setattr(
        local_text, "_call_local_text_model", lambda **kwargs: "地端翻譯"
    )

    handler = local_text.build_translate_handler(_settings(), target="zh")
    assert handler("これはペンです", "chat-1") == "地端翻譯"


def test_translate_handler_empty_input_returns_usage(monkeypatch) -> None:
    monkeypatch.setattr(
        command_bridge_providers,
        "generate_via_cloud_pool",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call cloud")),
    )
    handler = local_text.build_translate_handler(_settings(), target="zh")
    assert "用法" in handler("   ", "chat-1")
