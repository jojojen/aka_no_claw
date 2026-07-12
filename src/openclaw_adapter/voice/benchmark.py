"""Offline benchmark harness for voice embedding backends (#82 PR2, §14.3).

Evaluates a backend against a local, private manifest of recorded utterances —
nothing here ships audio anywhere. The harness answers the questions design
§21 leaves to benchmarking: which backend, and what direct/clarify thresholds.

Protocol: leave-one-out nearest-prototype retrieval. Each labeled sample is
scored against prototypes formed from the OTHER samples of every action;
unknown samples (expected kind = fallback) measure open-set false accepts.

Manifest entry (design §14.3):

    {"sample_id": "fan-off-001", "audio_path": "private/fan1.webm",
     "expected": {"kind": "clarify", "selected_action_id": "ir.fan.power"},
     "environment": "quiet", "session": "s1"}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .embedding import VoiceEmbeddingBackend, cosine_similarity


@dataclass(frozen=True)
class BenchmarkSample:
    sample_id: str
    audio_path: str
    expected_kind: str
    expected_action_id: str | None
    environment: str = ""
    session: str = ""


@dataclass
class BenchmarkReport:
    model_version: str
    known_total: int = 0
    top1_correct: int = 0
    topk_correct: int = 0
    unknown_total: int = 0
    false_accepts: int = 0
    per_sample: list[dict[str, object]] = field(default_factory=list)

    @property
    def top1_accuracy(self) -> float:
        return self.top1_correct / self.known_total if self.known_total else 0.0

    @property
    def topk_accuracy(self) -> float:
        return self.topk_correct / self.known_total if self.known_total else 0.0

    @property
    def false_accept_rate(self) -> float:
        return self.false_accepts / self.unknown_total if self.unknown_total else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "model_version": self.model_version,
            "known_total": self.known_total,
            "top1_accuracy": round(self.top1_accuracy, 4),
            "topk_accuracy": round(self.topk_accuracy, 4),
            "unknown_total": self.unknown_total,
            "false_accept_rate": round(self.false_accept_rate, 4),
        }


def load_manifest(path: str | Path) -> list[BenchmarkSample]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("benchmark manifest must be a JSON list")
    samples: list[BenchmarkSample] = []
    for entry in raw:
        expected = entry.get("expected") or {}
        samples.append(
            BenchmarkSample(
                sample_id=str(entry["sample_id"]),
                audio_path=str(entry["audio_path"]),
                expected_kind=str(expected.get("kind") or "fallback"),
                expected_action_id=(
                    str(expected["selected_action_id"])
                    if expected.get("selected_action_id")
                    else None
                ),
                environment=str(entry.get("environment") or ""),
                session=str(entry.get("session") or ""),
            )
        )
    return samples


def run_benchmark(
    samples: list[BenchmarkSample],
    backend: VoiceEmbeddingBackend,
    *,
    base_dir: str | Path = ".",
    top_k: int = 3,
    accept_threshold: float = 0.8,
) -> BenchmarkReport:
    base = Path(base_dir)
    embedded: list[tuple[BenchmarkSample, list[float]]] = []
    for sample in samples:
        audio = (base / sample.audio_path).read_bytes()
        embedded.append((sample, backend.embed(audio)))

    report = BenchmarkReport(model_version=backend.model_version)
    for i, (sample, vector) in enumerate(embedded):
        # Leave-one-out prototype set: every other labeled sample.
        scores: dict[str, float] = {}
        for j, (other, other_vec) in enumerate(embedded):
            if j == i or other.expected_action_id is None:
                continue
            score = cosine_similarity(vector, other_vec)
            prev = scores.get(other.expected_action_id)
            if prev is None or score > prev:
                scores[other.expected_action_id] = score
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best_action = ranked[0][0] if ranked else None
        best_score = ranked[0][1] if ranked else 0.0

        if sample.expected_action_id is not None:
            report.known_total += 1
            if best_action == sample.expected_action_id:
                report.top1_correct += 1
            if sample.expected_action_id in [a for a, _ in ranked[:top_k]]:
                report.topk_correct += 1
        else:
            report.unknown_total += 1
            if best_score >= accept_threshold:
                report.false_accepts += 1
        report.per_sample.append(
            {
                "sample_id": sample.sample_id,
                "expected_action_id": sample.expected_action_id,
                "best_action": best_action,
                "best_score": round(best_score, 4),
            }
        )
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .embedding import (
        BACKEND_SYNTHETIC,
        BACKEND_WHISPER_ENCODER,
        SyntheticEmbeddingBackend,
        WhisperEncoderEmbeddingBackend,
    )
    from .policy import DIRECT_SIMILARITY_THRESHOLD

    parser = argparse.ArgumentParser(description="Voice embedding benchmark (#82)")
    parser.add_argument("manifest", help="path to benchmark manifest JSON")
    parser.add_argument("--base-dir", default=".", help="root for audio_path entries")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--accept-threshold", type=float, default=DIRECT_SIMILARITY_THRESHOLD)
    parser.add_argument(
        "--backend",
        default=BACKEND_SYNTHETIC,
        choices=[BACKEND_SYNTHETIC, BACKEND_WHISPER_ENCODER],
    )
    parser.add_argument("--whisper-model", default="base")
    parser.add_argument("--whisper-download-root", default=".openclaw_tmp/whisper")
    args = parser.parse_args(argv)

    if args.backend == BACKEND_WHISPER_ENCODER:
        backend = WhisperEncoderEmbeddingBackend(
            model_name=args.whisper_model,
            device="auto",
            compute_type="default",
            download_root=args.whisper_download_root,
        )
    else:
        backend = SyntheticEmbeddingBackend()

    samples = load_manifest(args.manifest)
    report = run_benchmark(
        samples,
        backend,
        base_dir=args.base_dir,
        top_k=args.top_k,
        accept_threshold=args.accept_threshold,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
