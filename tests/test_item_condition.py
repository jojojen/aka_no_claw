"""Tests for item_condition.py."""

from __future__ import annotations

import json
from typing import Callable

import pytest

from openclaw_adapter.item_condition import (
    ConditionAssessment,
    _CONDITION_ASSESSMENT_PROMPT,
    _parse_condition_assessment_json,
    build_item_condition_assessor,
)


def test_prompt_contains_title_and_condition() -> None:
    """Test that the prompt template includes title and claimed condition."""
    prompt = _CONDITION_ASSESSMENT_PROMPT.format(
        title="テストカード",
        claimed_condition="目立った傷や汚れなし",
    )
    assert "テストカード" in prompt
    assert "目立った傷や汚れなし" in prompt


def test_parse_json_strict() -> None:
    """Test parsing strict JSON response."""
    json_str = (
        '{"summary": "良好な状態", "flaws": ["小さな傷"], "consistency": "consistent"}'
    )
    result = _parse_condition_assessment_json(json_str)
    assert result is not None
    assert result.summary == "良好な状態"
    assert result.flaws == ("小さな傷",)
    assert result.consistency == "consistent"


def test_parse_json_code_fenced() -> None:
    """Test parsing JSON wrapped in markdown code fence."""
    json_str = (
        "その他の説明\n```json\n"
        '{"summary": "状態に注意", "flaws": ["傷", "汚れ"], "consistency": "mismatch"}'
        "\n```\n追加説明"
    )
    result = _parse_condition_assessment_json(json_str)
    assert result is not None
    assert result.summary == "状態に注意"
    assert result.flaws == ("傷", "汚れ")
    assert result.consistency == "mismatch"


def test_parse_json_invalid_returns_none() -> None:
    """Test that malformed JSON returns None."""
    result = _parse_condition_assessment_json("not json at all")
    assert result is None


def test_parse_json_unknown_consistency() -> None:
    """Test that unknown consistency values default to 'unknown'."""
    json_str = (
        '{"summary": "不明", "flaws": [], "consistency": "invalid_value"}'
    )
    result = _parse_condition_assessment_json(json_str)
    assert result is not None
    assert result.consistency == "unknown"


def test_no_image_urls_returns_none() -> None:
    """Test that empty image_urls returns None without network access."""

    def fake_assessor(settings, **kwargs) -> Callable:
        def assess(title: str, condition_label: str | None, image_urls) -> ConditionAssessment | None:
            # Track that it was called
            assess.called = True
            return None
        assess.called = False
        return assess

    # Mock settings
    class FakeSettings:
        pass

    def mock_chain_fn():
        return []

    assessor = build_item_condition_assessor(
        FakeSettings(),
        acquire_images_fn=lambda urls, **kw: [],
        chain_fn=mock_chain_fn,
    )
    result = assessor("テスト", None, [])
    assert result is None


def test_no_successful_downloads_returns_none() -> None:
    """Test that failed image downloads return None."""

    def mock_acquire_images(urls: list[str], **kwargs) -> list[tuple[str, str]]:
        return []  # All downloads failed

    class FakeSettings:
        pass

    def mock_chain_fn():
        return []

    assessor = build_item_condition_assessor(
        FakeSettings(),
        acquire_images_fn=mock_acquire_images,
        chain_fn=mock_chain_fn,
    )
    result = assessor("テスト", None, ["http://example.com/image.jpg"])
    assert result is None


def test_all_providers_fail_returns_none() -> None:
    """Test that when walk_vision_pool_chain returns None text, assessment is None."""

    def mock_acquire_images(urls: list[str], **kwargs) -> list[tuple[str, str]]:
        return [("http://example.com/img.jpg", "base64data")]

    class FakeSettings:
        pass

    def mock_chain_fn():
        # Return a chain with fake providers that all fail
        def fake_build() -> None:
            return None

        def fake_configured() -> bool:
            return False

        return [
            ("fake_provider", "fake_model", fake_build, fake_configured),
        ]

    # Mock walk_vision_pool_chain to always return None
    import openclaw_adapter.item_condition as item_cond_module

    original_walk = item_cond_module.walk_vision_pool_chain

    def mock_walk(*args, **kwargs):
        return None, None, None, ()

    try:
        item_cond_module.walk_vision_pool_chain = mock_walk

        assessor = build_item_condition_assessor(
            FakeSettings(),
            acquire_images_fn=mock_acquire_images,
            chain_fn=mock_chain_fn,
        )
        result = assessor("テスト", None, ["http://example.com/image.jpg"])
        assert result is None
    finally:
        item_cond_module.walk_vision_pool_chain = original_walk


def test_successful_assessment() -> None:
    """Test successful vision assessment with JSON parsing."""

    def mock_acquire_images(urls: list[str], **kwargs) -> list[tuple[str, str]]:
        return [
            ("http://example.com/img1.jpg", "b64_1"),
            ("http://example.com/img2.jpg", "b64_2"),
        ]

    class FakeSettings:
        pass

    def mock_chain_fn():
        return []

    # Mock walk_vision_pool_chain to return valid JSON
    import openclaw_adapter.item_condition as item_cond_module

    original_walk = item_cond_module.walk_vision_pool_chain

    def mock_walk(*args, **kwargs):
        json_response = (
            '{"summary": "良好な状態です", "flaws": ["軽い傷"], "consistency": "consistent"}'
        )
        return json_response, "test_provider", "test_model", ()

    try:
        item_cond_module.walk_vision_pool_chain = mock_walk

        assessor = build_item_condition_assessor(
            FakeSettings(),
            acquire_images_fn=mock_acquire_images,
            chain_fn=mock_chain_fn,
        )
        result = assessor(
            "テストカード",
            "目立った傷や汚れなし",
            ["http://example.com/img1.jpg", "http://example.com/img2.jpg"],
        )
        assert result is not None
        assert result.summary == "良好な状態です"
        assert result.flaws == ("軽い傷",)
        assert result.consistency == "consistent"
        assert result.image_count == 2
        assert result.provider == "test_provider"
        assert result.model == "test_model"
    finally:
        item_cond_module.walk_vision_pool_chain = original_walk


def test_fallback_on_malformed_json() -> None:
    """Test fallback to raw text when JSON parsing fails."""

    def mock_acquire_images(urls: list[str], **kwargs) -> list[tuple[str, str]]:
        return [("http://example.com/img.jpg", "b64")]

    class FakeSettings:
        pass

    def mock_chain_fn():
        return []

    import openclaw_adapter.item_condition as item_cond_module

    original_walk = item_cond_module.walk_vision_pool_chain

    def mock_walk(*args, **kwargs):
        # Return non-JSON text
        return "This is not JSON, just raw text from the model", "fallback_provider", "fallback_model", ()

    try:
        item_cond_module.walk_vision_pool_chain = mock_walk

        assessor = build_item_condition_assessor(
            FakeSettings(),
            acquire_images_fn=mock_acquire_images,
            chain_fn=mock_chain_fn,
        )
        result = assessor("テスト", None, ["http://example.com/img.jpg"])
        assert result is not None
        assert "not JSON" in result.summary
        assert result.consistency == "unknown"
        assert result.image_count == 1
        assert result.provider == "fallback_provider"
    finally:
        item_cond_module.walk_vision_pool_chain = original_walk


def test_condition_assessment_with_mismatch() -> None:
    """Test assessment when condition doesn't match seller's claim."""

    def mock_acquire_images(urls: list[str], **kwargs) -> list[tuple[str, str]]:
        return [("http://example.com/img.jpg", "b64")]

    class FakeSettings:
        pass

    def mock_chain_fn():
        return []

    import openclaw_adapter.item_condition as item_cond_module

    original_walk = item_cond_module.walk_vision_pool_chain

    def mock_walk(*args, **kwargs):
        json_response = (
            '{"summary": "状態が悪い", "flaws": ["大きな傷", "汚れ"], "consistency": "mismatch"}'
        )
        return json_response, "test_provider", "test_model", ()

    try:
        item_cond_module.walk_vision_pool_chain = mock_walk

        assessor = build_item_condition_assessor(
            FakeSettings(),
            acquire_images_fn=mock_acquire_images,
            chain_fn=mock_chain_fn,
        )
        result = assessor(
            "テストカード",
            "目立った傷や汚れなし",  # Seller claimed this, but it's wrong
            ["http://example.com/img.jpg"],
        )
        assert result is not None
        assert result.consistency == "mismatch"
    finally:
        item_cond_module.walk_vision_pool_chain = original_walk
