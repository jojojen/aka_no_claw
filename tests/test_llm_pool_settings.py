"""Tests for the cloud-pool rotation cursor (chat-goal loop follow-up)."""

from __future__ import annotations

from openclaw_adapter.llm_pool_settings import CloudPoolRotation


def test_cloud_pool_rotation_starts_unrotated():
    rotation = CloudPoolRotation()
    assert rotation.rotate(["a", "b", "c"]) == ["a", "b", "c"]


def test_cloud_pool_rotation_advances_cursor_each_call():
    rotation = CloudPoolRotation()
    assert rotation.rotate(["a", "b", "c"]) == ["a", "b", "c"]
    assert rotation.rotate(["a", "b", "c"]) == ["b", "c", "a"]
    assert rotation.rotate(["a", "b", "c"]) == ["c", "a", "b"]
    assert rotation.rotate(["a", "b", "c"]) == ["a", "b", "c"]


def test_cloud_pool_rotation_handles_empty_list():
    rotation = CloudPoolRotation()
    assert rotation.rotate([]) == []
    # An empty rotate must not advance the cursor or affect later calls.
    assert rotation.rotate(["x", "y"]) == ["x", "y"]


def test_cloud_pool_rotation_handles_single_item():
    rotation = CloudPoolRotation()
    assert rotation.rotate(["only"]) == ["only"]
    assert rotation.rotate(["only"]) == ["only"]


def test_cloud_pool_rotation_tolerates_changing_list_size():
    """Provider counts can change between calls (settings edited mid-run is not
    a real scenario, but the cursor should never index out of range)."""
    rotation = CloudPoolRotation()
    rotation.rotate(["a", "b", "c"])  # cursor -> 1
    assert rotation.rotate(["x", "y"]) == ["y", "x"]


def test_vision_pool_never_offers_big_pickle(tmp_path):
    """big_pickle is text-only (opencode gateway rejects image payloads with
    HTTP 400, verified live 2026-07-05): a config that lists it in vision_pool
    must be filtered out, and the settings payload must not offer it as a
    vision provider/model option."""
    import json
    from types import SimpleNamespace

    from openclaw_adapter.llm_pool_settings import (
        LLM_PROVIDER_BIG_PICKLE,
        chat_llm_pool_payload,
        load_chat_llm_pool_settings,
    )

    config = tmp_path / "llm_pool.json"
    config.write_text(json.dumps({
        "default_chat_provider": "cloud_pool",
        "cloud_pool": ["gemini", "big_pickle"],
        "vision_pool": ["gemini", "big_pickle", "local"],
        "vision_providers": {"big_pickle": {"enabled": True, "model": "big-pickle"}},
    }), encoding="utf-8")
    settings = SimpleNamespace(openclaw_llm_pool_config_path=str(config))

    loaded = load_chat_llm_pool_settings(settings)
    assert LLM_PROVIDER_BIG_PICKLE not in loaded.vision_pool
    assert LLM_PROVIDER_BIG_PICKLE not in (loaded.vision_providers or {})
    # text pool is unaffected
    assert LLM_PROVIDER_BIG_PICKLE in loaded.cloud_pool

    payload = chat_llm_pool_payload(settings)
    assert LLM_PROVIDER_BIG_PICKLE not in payload["vision_pool"]
    assert LLM_PROVIDER_BIG_PICKLE not in payload["vision_providers"]
    assert LLM_PROVIDER_BIG_PICKLE not in payload["vision_model_options"]


def test_vision_pool_includes_nvidia_by_default():
    """nvidia vision (meta/llama-3.2-11b/90b-vision-instruct) was live-verified
    against this account's own NVIDIA_KEY on 2026-07-07 with a real image
    payload, so it must be offered as a vision-pool provider by default."""
    from types import SimpleNamespace

    from openclaw_adapter.llm_pool_settings import (
        LLM_PROVIDER_NVIDIA,
        chat_llm_pool_payload,
        default_chat_llm_pool_settings,
    )

    settings = SimpleNamespace(openclaw_nvidia_api_key="test-key")

    defaults = default_chat_llm_pool_settings(settings)
    assert LLM_PROVIDER_NVIDIA in defaults.vision_pool
    assert LLM_PROVIDER_NVIDIA in (defaults.vision_providers or {})

    payload = chat_llm_pool_payload(settings)
    assert LLM_PROVIDER_NVIDIA in payload["vision_pool"]
    assert payload["vision_providers"][LLM_PROVIDER_NVIDIA]["configured"] is True
    assert "meta/llama-3.2-11b-vision-instruct" in payload["vision_model_options"][LLM_PROVIDER_NVIDIA]
    assert "meta/llama-3.2-90b-vision-instruct" in payload["vision_model_options"][LLM_PROVIDER_NVIDIA]
