from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from assistant_runtime import AssistantSettings, build_ssl_context

_LOGGER = logging.getLogger(__name__)

# Envelope versions this client can interpret (aka_no_claw#77 D2.4). A
# response with NO envelope_version is the legacy/implicit-v0 case and stays
# accepted during the compatibility window.
SUPPORTED_ENVELOPE_VERSIONS = frozenset({1})


class IncompatibleEnvelopeError(RuntimeError):
    """Server responded with an envelope version this client does not
    support. Must not be interpreted as unavailable or empty."""


class CorruptResponseError(RuntimeError):
    """Server response could not be parsed as the expected JSON object.
    Must not be interpreted as unavailable or empty."""


class RateLimitedError(RuntimeError):
    """Server explicitly throttled the request (HTTP 429)."""


class SnapshotStillPending(RuntimeError):
    """Raised when a job did not complete within the initial poll budget.

    Carries poll_fn so callers can background-track the job and deliver
    the result asynchronously without discarding it.
    """

    def __init__(
        self,
        job_id: str,
        poll_fn: "Callable[[], ReputationSnapshotResult | None]",
    ) -> None:
        super().__init__(
            f"Snapshot job {job_id} is still pending (background tracking available)"
        )
        self.job_id = job_id
        self.poll_fn = poll_fn


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
        _LOGGER.info("reputation snapshot job created job_id=%s", job_id)
        return self._wait_for_job(job_id)

    def _wait_for_job(self, job_id: str) -> ReputationSnapshotResult:
        deadline = time.monotonic() + self._job_timeout_seconds
        while time.monotonic() < deadline:
            try:
                payload = self._request_json(f"/api/jobs/{job_id}", method="GET")
            except Exception as exc:
                # A transient poll failure (e.g. socket timeout while the agent is
                # busy serializing captures) must NOT be reported as a permanent
                # snapshot failure. Tolerate it and let the budget fall through to
                # SnapshotStillPending so background follow-up is scheduled.
                _LOGGER.warning(
                    "reputation snapshot poll request failed job_id=%s, retrying: %s",
                    job_id,
                    exc,
                )
                time.sleep(self._poll_interval_seconds)
                continue
            status = str(payload.get("status") or "").strip().lower()
            if status == "done":
                return self._build_result(payload, job_id=job_id)
            if status == "failed":
                error = str(payload.get("error") or "Unknown reputation snapshot error.")
                raise RuntimeError(error)
            time.sleep(self._poll_interval_seconds)
        _LOGGER.info(
            "reputation snapshot job still pending after %.0fs budget job_id=%s; "
            "registering background follow-up",
            self._job_timeout_seconds,
            job_id,
        )
        raise SnapshotStillPending(
            job_id,
            poll_fn=lambda: self._poll_until_done(job_id, max_seconds=900.0),
        )

    def _poll_until_done(
        self, job_id: str, *, max_seconds: float
    ) -> "ReputationSnapshotResult | None":
        """Keep polling until done/failed/cap; tolerates transient HTTP errors.

        Returns None on timeout or explicit failure (not an exception) so the
        background thread can send a clean "timed out" message rather than crash.
        """
        deadline = time.monotonic() + max_seconds
        while time.monotonic() < deadline:
            try:
                payload = self._request_json(f"/api/jobs/{job_id}", method="GET")
            except Exception:
                time.sleep(self._poll_interval_seconds)
                continue
            status = str(payload.get("status") or "").strip().lower()
            if status == "done":
                return self._build_result(payload, job_id=job_id)
            if status == "failed":
                return None
            time.sleep(self._poll_interval_seconds)
        return None

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
                raw = response.read().decode("utf-8")
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
            if exc.code == 429:
                raise RateLimitedError(
                    f"reputation_snapshot rate limited (HTTP 429): {detail}"
                ) from exc
            raise RuntimeError(f"reputation_snapshot HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"reputation_snapshot request failed: {exc.reason}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CorruptResponseError(
                "reputation_snapshot returned malformed JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise CorruptResponseError("reputation_snapshot returned a non-object response.")

        # Missing envelope_version = legacy v0, accepted during the
        # compatibility window (#77 D2.4 migration sequence).
        envelope_version = payload.get("envelope_version")
        if envelope_version is not None and envelope_version not in SUPPORTED_ENVELOPE_VERSIONS:
            raise IncompatibleEnvelopeError(
                f"reputation_snapshot envelope_version {envelope_version!r} unsupported "
                f"(supported: {sorted(SUPPORTED_ENVELOPE_VERSIONS)})"
            )
        return payload


def request_reputation_snapshot(
    *,
    settings: AssistantSettings,
    query_url: str,
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 2.0,
    job_timeout_seconds: float | None = None,
) -> ReputationSnapshotResult:
    if job_timeout_seconds is None:
        job_timeout_seconds = settings.reputation_agent_job_timeout_secs
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
