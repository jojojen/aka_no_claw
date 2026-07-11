from __future__ import annotations

from assistant_runtime import AssistantSettings

from openclaw_adapter.reputation_snapshot import (
    ReputationSnapshotClient,
    SnapshotStillPending,
    request_reputation_snapshot,
)


def _settings() -> AssistantSettings:
    return AssistantSettings(reputation_agent_server_url="http://127.0.0.1:5000")


def test_reputation_snapshot_client_reuses_existing_snapshot(monkeypatch) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request_json(self, path: str, *, method: str, body: dict[str, object] | None = None) -> dict[str, object]:
        calls.append((path, method, body))
        return {
            "proof_id": "proof_existing",
            "proof_url": "/p/proof_existing",
            "reused": True,
        }

    monkeypatch.setattr(ReputationSnapshotClient, "_request_json", fake_request_json)
    client = ReputationSnapshotClient(settings=_settings(), poll_interval_seconds=0.0, job_timeout_seconds=1.0)

    result = client.create_or_reuse_snapshot("https://jp.mercari.com/item/m123456789")

    assert calls == [
        ("/api/captures", "POST", {"query_url": "https://jp.mercari.com/item/m123456789"}),
    ]
    assert result.reused is True
    assert result.proof_id == "proof_existing"
    assert result.proof_url == "http://127.0.0.1:5000/p/proof_existing"


def test_reputation_snapshot_client_polls_until_job_finishes(monkeypatch) -> None:
    responses = iter(
        [
            {"job_id": "job_123", "status": "pending"},
            {"job_id": "job_123", "status": "pending"},
            {"proof_id": "proof_new", "proof_url": "/p/proof_new", "status": "done"},
        ]
    )

    def fake_request_json(self, path: str, *, method: str, body: dict[str, object] | None = None) -> dict[str, object]:
        response = next(responses)
        if path == "/api/captures":
            assert method == "POST"
            assert body == {"query_url": "https://jp.mercari.com/user/profile/demo_seller"}
        else:
            assert path == "/api/jobs/job_123"
            assert method == "GET"
            assert body is None
        return response

    monkeypatch.setattr(ReputationSnapshotClient, "_request_json", fake_request_json)
    client = ReputationSnapshotClient(settings=_settings(), poll_interval_seconds=0.0, job_timeout_seconds=1.0)

    result = client.create_or_reuse_snapshot("https://jp.mercari.com/user/profile/demo_seller")

    assert result.reused is False
    assert result.job_id == "job_123"
    assert result.proof_id == "proof_new"
    assert result.proof_url == "http://127.0.0.1:5000/p/proof_new"


def test_reputation_snapshot_client_times_out_raises_snapshot_still_pending(monkeypatch) -> None:
    def fake_request_json(self, path: str, *, method: str, body: dict[str, object] | None = None) -> dict[str, object]:
        assert path == "/api/captures"
        assert method == "POST"
        return {"job_id": "job_123", "status": "pending"}

    monkeypatch.setattr(ReputationSnapshotClient, "_request_json", fake_request_json)
    client = ReputationSnapshotClient(settings=_settings(), poll_interval_seconds=0.0, job_timeout_seconds=0.0)

    import pytest
    with pytest.raises(SnapshotStillPending) as exc_info:
        client.create_or_reuse_snapshot("https://jp.mercari.com/item/m123456789")
    assert exc_info.value.job_id == "job_123"
    assert callable(exc_info.value.poll_fn)


def test_wait_for_job_tolerates_transient_poll_timeout_then_pends(monkeypatch) -> None:
    # Regression (issue #6): a socket timeout during polling escaped _wait_for_job
    # as a generic RuntimeError("...timed out") and was rendered as a permanent
    # seller-snapshot failure, so no background follow-up was scheduled. The poll
    # loop must now tolerate the transient failure and fall through to
    # SnapshotStillPending once the budget is exhausted.
    calls = {"get": 0}

    def fake_request_json(self, path: str, *, method: str, body: dict[str, object] | None = None) -> dict[str, object]:
        if path == "/api/captures":
            return {"job_id": "job_t1", "status": "pending"}
        calls["get"] += 1
        raise RuntimeError("reputation_snapshot request failed: timed out")

    monkeypatch.setattr(ReputationSnapshotClient, "_request_json", fake_request_json)
    client = ReputationSnapshotClient(
        settings=_settings(), poll_interval_seconds=0.0, job_timeout_seconds=0.05
    )

    import pytest
    with pytest.raises(SnapshotStillPending) as exc_info:
        client.create_or_reuse_snapshot("https://jp.mercari.com/item/m123456789")
    assert exc_info.value.job_id == "job_t1"
    assert calls["get"] >= 1  # we actually polled and tolerated the timeout


def test_request_reputation_snapshot_uses_settings_job_timeout(monkeypatch) -> None:
    # Regression: the hard-coded 90 s client default gave up before slow jobs
    # (~133 s observed) finished. The poll budget must come from settings.
    captured: dict[str, float] = {}

    real_init = ReputationSnapshotClient.__init__

    def spy_init(self, *, settings, timeout_seconds=30.0, poll_interval_seconds=2.0, job_timeout_seconds=90.0):
        captured["job_timeout_seconds"] = job_timeout_seconds
        real_init(
            self,
            settings=settings,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            job_timeout_seconds=job_timeout_seconds,
        )

    def fake_request_json(self, path: str, *, method: str, body: dict[str, object] | None = None) -> dict[str, object]:
        return {"proof_id": "p1", "proof_url": "/p/p1", "reused": False}

    monkeypatch.setattr(ReputationSnapshotClient, "__init__", spy_init)
    monkeypatch.setattr(ReputationSnapshotClient, "_request_json", fake_request_json)

    settings = AssistantSettings(
        reputation_agent_server_url="http://127.0.0.1:5000",
        reputation_agent_job_timeout_secs=300.0,
    )
    request_reputation_snapshot(
        settings=settings,
        query_url="https://jp.mercari.com/item/m123456789",
    )

    assert captured["job_timeout_seconds"] == 300.0


def test_poll_until_done_returns_result_when_job_completes(monkeypatch) -> None:
    responses = iter([
        {"status": "processing"},
        {"status": "done", "proof_id": "proof_bg", "proof_url": "/p/proof_bg"},
    ])

    def fake_request_json(self, path: str, *, method: str, body=None):
        return next(responses)

    monkeypatch.setattr(ReputationSnapshotClient, "_request_json", fake_request_json)
    client = ReputationSnapshotClient(settings=_settings(), poll_interval_seconds=0.0)
    result = client._poll_until_done("job_bg", max_seconds=60.0)
    assert result is not None
    assert result.proof_id == "proof_bg"


def test_poll_until_done_returns_none_on_failure(monkeypatch) -> None:
    def fake_request_json(self, path: str, *, method: str, body=None):
        return {"status": "failed", "error": "Mercari blocked"}

    monkeypatch.setattr(ReputationSnapshotClient, "_request_json", fake_request_json)
    client = ReputationSnapshotClient(settings=_settings(), poll_interval_seconds=0.0)
    result = client._poll_until_done("job_fail", max_seconds=60.0)
    assert result is None


def test_poll_until_done_returns_none_on_hard_cap(monkeypatch) -> None:
    def fake_request_json(self, path: str, *, method: str, body=None):
        return {"status": "processing"}

    monkeypatch.setattr(ReputationSnapshotClient, "_request_json", fake_request_json)
    client = ReputationSnapshotClient(settings=_settings(), poll_interval_seconds=0.0)
    result = client._poll_until_done("job_cap", max_seconds=0.0)
    assert result is None


def test_poll_until_done_tolerates_transient_http_errors(monkeypatch) -> None:
    calls = [0]

    def fake_request_json(self, path: str, *, method: str, body=None):
        calls[0] += 1
        if calls[0] < 3:
            raise RuntimeError("transient")
        return {"status": "done", "proof_id": "proof_ok", "proof_url": "/p/proof_ok"}

    monkeypatch.setattr(ReputationSnapshotClient, "_request_json", fake_request_json)
    client = ReputationSnapshotClient(settings=_settings(), poll_interval_seconds=0.0)
    result = client._poll_until_done("job_retry", max_seconds=60.0)
    assert result is not None
    assert result.proof_id == "proof_ok"


# ── D2.4 (aka_no_claw#77): envelope versioning + failure-state parsing ──────
# These mock urlopen (not _request_json) so the real parsing path runs.

import io
import json as _json

import pytest

import openclaw_adapter.reputation_snapshot as reputation_snapshot_module
from openclaw_adapter.reputation_snapshot import (
    CorruptResponseError,
    IncompatibleEnvelopeError,
    RateLimitedError,
    SUPPORTED_ENVELOPE_VERSIONS,
)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _client_with_body(monkeypatch, body: bytes) -> ReputationSnapshotClient:
    monkeypatch.setattr(
        reputation_snapshot_module,
        "urlopen",
        lambda *args, **kwargs: _FakeResponse(body),
    )
    return ReputationSnapshotClient(settings=_settings())


def test_request_json_accepts_envelope_version_1(monkeypatch) -> None:
    body = _json.dumps({"proof_url": "/p/abc", "envelope_version": 1}).encode()
    client = _client_with_body(monkeypatch, body)
    payload = client._request_json("/api/proofs/abc", method="GET")
    assert payload["proof_url"] == "/p/abc"


def test_request_json_accepts_missing_envelope_version_as_legacy(monkeypatch) -> None:
    body = _json.dumps({"proof_url": "/p/abc"}).encode()
    client = _client_with_body(monkeypatch, body)
    payload = client._request_json("/api/proofs/abc", method="GET")
    assert payload["proof_url"] == "/p/abc"


def test_request_json_rejects_unsupported_envelope_version(monkeypatch) -> None:
    body = _json.dumps({"proof_url": "/p/abc", "envelope_version": 99}).encode()
    client = _client_with_body(monkeypatch, body)
    with pytest.raises(IncompatibleEnvelopeError, match="99"):
        client._request_json("/api/proofs/abc", method="GET")


def test_request_json_rejects_malformed_json_as_corrupt(monkeypatch) -> None:
    client = _client_with_body(monkeypatch, b"<html>not json</html>")
    with pytest.raises(CorruptResponseError, match="malformed"):
        client._request_json("/api/proofs/abc", method="GET")


def test_request_json_rejects_non_object_response_as_corrupt(monkeypatch) -> None:
    client = _client_with_body(monkeypatch, b"[1, 2, 3]")
    with pytest.raises(CorruptResponseError, match="non-object"):
        client._request_json("/api/proofs/abc", method="GET")


def test_request_json_maps_http_429_to_rate_limited(monkeypatch) -> None:
    from urllib.error import HTTPError

    def raise_429(*args, **kwargs):
        raise HTTPError(
            "http://x", 429, "Too Many Requests", hdrs=None, fp=io.BytesIO(b"")
        )

    monkeypatch.setattr(reputation_snapshot_module, "urlopen", raise_429)
    client = ReputationSnapshotClient(settings=_settings())
    with pytest.raises(RateLimitedError, match="429"):
        client._request_json("/api/captures", method="POST", body={})


def test_supported_envelope_versions_contains_v1() -> None:
    assert 1 in SUPPORTED_ENVELOPE_VERSIONS
