"""In-process outcome counters and stage timings for the voice gate
(design §14.1/§14.2, #82 PR4).

Deliberately tiny: a thread-safe counter map plus per-stage latency
aggregates, snapshotable over HTTP for benchmarking. Action IDs are low
cardinality here (one household); a real metrics backend can re-aggregate
by surface/risk later (§14.2).
"""

from __future__ import annotations

import threading
from collections import Counter


class VoiceMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._stage_ms: dict[str, dict[str, float]] = {}

    def record_resolution(self, kind: str, reason_code: str = "") -> None:
        self._inc(f"voice_resolution_total{{kind={kind},reason={reason_code}}}")

    def record_direct_action(self, action_id: str, result: str) -> None:
        self._inc(f"voice_direct_action_total{{action={action_id},result={result}}}")

    def record_negative_correction(self, action_id: str) -> None:
        self._inc(f"voice_negative_correction_total{{action={action_id}}}")

    def record_learning_commit(self, result: str) -> None:
        self._inc(f"voice_learning_commit_total{{result={result}}}")

    def observe_stage(self, stage: str, elapsed_ms: float) -> None:
        with self._lock:
            agg = self._stage_ms.setdefault(
                stage, {"count": 0.0, "total_ms": 0.0, "last_ms": 0.0}
            )
            agg["count"] += 1
            agg["total_ms"] += elapsed_ms
            agg["last_ms"] = elapsed_ms

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "stages": {k: dict(v) for k, v in self._stage_ms.items()},
            }

    def _inc(self, key: str) -> None:
        with self._lock:
            self._counters[key] += 1


# Process-wide singleton; the bridge is one process, tests may replace it.
METRICS = VoiceMetrics()
