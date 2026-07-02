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
