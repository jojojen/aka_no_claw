from __future__ import annotations

import json
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from assistant_runtime import AssistantSettings, build_ssl_context


@dataclass(frozen=True, slots=True)
class ReputationSnapshotResult:
    proof_url: str
    proof_id: str | None
    reused: bool
    job_id: str | None = None


class ReputationSnapshotClient:
    def __init__(
        self,
        *,
        settings: AssistantSettings,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 2.0,
        job_timeout_seconds: float = 90.0,
    ) -> None:
        self._server_url = settings.reputation_agent_server_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._job_timeout_seconds = job_timeout_seconds
        self._ssl_context = build_ssl_context(settings)

    def create_or_reuse_snapshot(self, query_url: str) -> ReputationSnapshotResult:
        payload = self._request_json(
            "/api/captures",
            method="POST",
            body={"query_url": query_url},
        )
        if "proof_url" in payload:
            return self._build_result(payload)

        job_id = str(payload.get("job_id") or "").strip()
        if not job_id:
            raise RuntimeError("reputation_snapshot did not return a proof link or job id.")
        return self._wait_for_job(job_id)

    def _wait_for_job(self, job_id: str) -> ReputationSnapshotResult:
        deadline = time.monotonic() + self._job_timeout_seconds
        while time.monotonic() < deadline:
            payload = self._request_json(f"/api/jobs/{job_id}", method="GET")
            status = str(payload.get("status") or "").strip().lower()
            if status == "done":
                return self._build_result(payload, job_id=job_id)
            if status == "failed":
                error = str(payload.get("error") or "Unknown reputation snapshot error.")
                raise RuntimeError(error)
            time.sleep(self._poll_interval_seconds)
        raise RuntimeError(
            "Snapshot job is still pending. Make sure a reputation agent is running, "
            "for example start OpenClaw with --with-reputation-agent."
        )

    def _build_result(
        self,
        payload: dict[str, object],
        *,
        job_id: str | None = None,
    ) -> ReputationSnapshotResult:
        proof_url = str(payload.get("proof_url") or "").strip()
        if not proof_url:
            raise RuntimeError("reputation_snapshot response is missing proof_url.")
        resolved_proof_url = urljoin(f"{self._server_url}/", proof_url.lstrip("/"))
        proof_id_raw = payload.get("proof_id")
        proof_id = None if proof_id_raw in {None, ""} else str(proof_id_raw)
        if proof_id is None:
            proof_id = _extract_proof_id_from_url(resolved_proof_url)
        return ReputationSnapshotResult(
            proof_url=resolved_proof_url,
            proof_id=proof_id,
            reused=bool(payload.get("reused")),
            job_id=job_id,
        )

    def get_proof_document(self, proof_id: str) -> dict[str, object]:
        return self._request_json(f"/api/proofs/{proof_id}", method="GET")

    def _request_json(
        self,
        path: str,
        *,
        method: str,
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        request_body = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            f"{self._server_url}{path}",
            data=request_body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method=method,
        )
        try:
            with urlopen(
                request,
                timeout=self._timeout_seconds,
                context=self._ssl_context,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.reason
            try:
                body_text = exc.read().decode("utf-8")
            except Exception:
                body_text = ""
            if body_text:
                try:
                    parsed = json.loads(body_text)
                except json.JSONDecodeError:
                    detail = body_text
                else:
                    detail = parsed.get("error") or parsed
            raise RuntimeError(f"reputation_snapshot HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"reputation_snapshot request failed: {exc.reason}") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("reputation_snapshot returned a non-object response.")
        return payload


def request_reputation_snapshot(
    *,
    settings: AssistantSettings,
    query_url: str,
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 2.0,
    job_timeout_seconds: float = 90.0,
) -> ReputationSnapshotResult:
    client = ReputationSnapshotClient(
        settings=settings,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        job_timeout_seconds=job_timeout_seconds,
    )
    return client.create_or_reuse_snapshot(query_url)


def fetch_reputation_proof_document(
    *,
    settings: AssistantSettings,
    proof_id: str,
    timeout_seconds: float = 30.0,
) -> dict[str, object]:
    client = ReputationSnapshotClient(
        settings=settings,
        timeout_seconds=timeout_seconds,
    )
    return client.get_proof_document(proof_id)


def _extract_proof_id_from_url(proof_url: str) -> str | None:
    path = urlparse(proof_url).path.strip("/")
    if not path:
        return None
    parts = path.split("/")
    if len(parts) >= 2 and parts[-2] == "p":
        return parts[-1] or None
    return None
