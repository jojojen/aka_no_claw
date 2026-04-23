from __future__ import annotations

from assistant_runtime import AssistantSettings

from openclaw_adapter.reputation_snapshot import ReputationSnapshotClient


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


def test_reputation_snapshot_client_times_out_with_agent_hint(monkeypatch) -> None:
    def fake_request_json(self, path: str, *, method: str, body: dict[str, object] | None = None) -> dict[str, object]:
        assert path == "/api/captures"
        assert method == "POST"
        return {"job_id": "job_123", "status": "pending"}

    monkeypatch.setattr(ReputationSnapshotClient, "_request_json", fake_request_json)
    client = ReputationSnapshotClient(settings=_settings(), poll_interval_seconds=0.0, job_timeout_seconds=0.0)

    try:
        client.create_or_reuse_snapshot("https://jp.mercari.com/item/m123456789")
    except RuntimeError as exc:
        assert "reputation agent" in str(exc)
    else:  # pragma: no cover - defensive.
        raise AssertionError("Expected a timeout error when no reputation agent is available.")
