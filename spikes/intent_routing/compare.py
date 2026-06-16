"""End-to-end intent-routing spike: production LLM router (qwen3:14b) vs a
bge-m3 embedding nearest-intent router, on 20 real natural-language utterances.

Measures, per router: intent-label accuracy (hit@1) and per-utterance latency.
The embedding router additionally reports hit@3 + MRR over the intent ranking.

This compares ONLY the routing decision (which command). Slot-filling (price,
card number, @handle, schedule…) is out of scope here and is discussed
qualitatively in RESULTS.md — the embedding router does not extract slots.

Run:
    .venv/bin/python spikes/intent_routing/compare.py
"""
from __future__ import annotations

import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from assistant_runtime.settings import get_settings, load_dotenv  # noqa: E402
from openclaw_adapter.kb_embedder import OllamaEmbedder  # noqa: E402
from openclaw_adapter.natural_language import (  # noqa: E402
    build_telegram_natural_language_router_from_settings,
)

HERE = Path(__file__).resolve().parent
EMBED_MODEL = "bge-m3"
ENDPOINT = "http://127.0.0.1:11434"


def _norm(vec: list[float]) -> list[float] | None:
    n = math.sqrt(math.fsum(x * x for x in vec))
    if not n or not math.isfinite(n):
        return None
    return [x / n for x in vec]


def _dot(a: list[float], b: list[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b))


def build_embed_router(emb: OllamaEmbedder):
    index = json.loads((HERE / "intent_index.json").read_text(encoding="utf-8"))
    vecs: dict[str, list[list[float]]] = {}
    for intent, phrasings in index.items():
        rows = []
        for p in phrasings:
            v = emb(p)
            if v is not None:
                nv = _norm(v)
                if nv is not None:
                    rows.append(nv)
        vecs[intent] = rows

    def predict(text: str):
        qv = emb(text)
        if qv is None:
            return [], 0.0
        nq = _norm(qv)
        if nq is None:
            return [], 0.0
        scored = []
        for intent, rows in vecs.items():
            if not rows:
                continue
            best = max(_dot(nq, r) for r in rows)
            scored.append((intent, best))
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored, (scored[0][1] if scored else 0.0)

    return predict


def main() -> None:
    load_dotenv()
    settings = get_settings()
    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))

    emb = OllamaEmbedder(endpoint=ENDPOINT, model=EMBED_MODEL, timeout=60)
    embed_predict = build_embed_router(emb)

    llm_router = build_telegram_natural_language_router_from_settings(settings)
    llm_model = llm_router.model if llm_router else "(none)"
    print(f"embed_model={EMBED_MODEL} llm_router_model={llm_model}")

    rows = []
    emb_hit1 = emb_hit3 = 0
    emb_rr = []
    emb_lat = []
    llm_hit1 = 0
    llm_lat = []

    for c in cases:
        utt, exp = c["utterance"], c["expected"]

        t0 = time.time()
        scored, top_score = embed_predict(utt)
        emb_lat.append((time.time() - t0) * 1000)
        ranking = [i for i, _ in scored]
        emb_pred = ranking[0] if ranking else "(none)"
        e1 = exp == emb_pred
        e3 = exp in ranking[:3]
        emb_hit1 += int(e1)
        emb_hit3 += int(e3)
        emb_rr.append(1.0 / (ranking.index(exp) + 1) if exp in ranking else 0.0)

        llm_pred = "(error)"
        t0 = time.time()
        try:
            intent = llm_router.route(utt) if llm_router else None
            llm_pred = intent.intent if intent else "(none)"
        except Exception as exc:  # noqa: BLE001
            llm_pred = f"(error: {type(exc).__name__})"
        llm_lat.append((time.time() - t0) * 1000)
        l1 = exp == llm_pred
        llm_hit1 += int(l1)

        rows.append({
            "utt": utt, "exp": exp,
            "emb_pred": emb_pred, "emb_score": top_score, "e1": e1, "e3": e3,
            "llm_pred": llm_pred, "l1": l1,
            "llm_ms": llm_lat[-1], "emb_ms": emb_lat[-1],
        })
        print(f"  {exp:22s} emb={emb_pred:22s}{'OK' if e1 else 'XX'}  llm={llm_pred:22s}{'OK' if l1 else 'XX'}")

    n = len(cases)
    md = []
    md.append("# Intent routing spike — LLM router vs bge-m3 embedding router\n")
    md.append(f"- cases: **{n}** real natural-language utterances across {len({c['expected'] for c in cases})} distinct intents")
    md.append(f"- embedding router: **{EMBED_MODEL}** nearest-intent (cosine over canonical phrasings)")
    md.append(f"- LLM router (production): **{llm_model}** JSON-schema generation, temperature 0")
    md.append("- scope: routing decision only (which command). Slot-filling NOT measured here.\n")
    md.append("## Summary\n")
    md.append("| router | hit@1 | hit@3 | MRR | median latency | p90 latency |")
    md.append("|---|---|---|---|---|---|")

    def p90(xs):
        return sorted(xs)[max(0, math.ceil(0.9 * len(xs)) - 1)]

    md.append(
        f"| {EMBED_MODEL} embedding | {emb_hit1}/{n} | {emb_hit3}/{n} | "
        f"{statistics.mean(emb_rr):.3f} | {statistics.median(emb_lat):.0f} ms | {p90(emb_lat):.0f} ms |"
    )
    md.append(
        f"| {llm_model} LLM | {llm_hit1}/{n} | — | — | "
        f"{statistics.median(llm_lat):.0f} ms | {p90(llm_lat):.0f} ms |"
    )
    speedup = statistics.median(llm_lat) / max(1e-6, statistics.median(emb_lat))
    md.append(f"\nEmbedding median latency is **~{speedup:.0f}x** faster than the LLM router.\n")

    md.append("## Per-utterance\n")
    md.append("| # | utterance | expected | embed pred | score | LLM pred | LLM ms | emb ms |")
    md.append("|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        ep = r["emb_pred"] + (" ✅" if r["e1"] else " ❌")
        lp = r["llm_pred"] + (" ✅" if r["l1"] else " ❌")
        md.append(
            f"| {i} | {r['utt']} | {r['exp']} | {ep} | {r['emb_score']:.3f} | {lp} | "
            f"{r['llm_ms']:.0f} | {r['emb_ms']:.0f} |"
        )

    (HERE / "RESULTS.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\nwrote {HERE / 'RESULTS.md'}")
    print(f"embed hit@1={emb_hit1}/{n} hit@3={emb_hit3}/{n}  llm hit@1={llm_hit1}/{n}")


if __name__ == "__main__":
    main()
