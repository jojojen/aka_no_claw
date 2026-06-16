"""可拔插的 embedding 檢索 spike：比較 lexical baseline vs 多個 embedding 模型。

只讀 knowledge.sqlite3，不接 production，不改任何 bot 程式碼。
跑法： .venv/bin/python spikes/embedding_retrieval/compare.py
產出： spikes/embedding_retrieval/RESULTS.md

模型設定見 MODELS：nomic-embed-text 需要 search_query/search_document 前綴；
bge-m3 是多語模型、不需前綴。lexical 是 char-bigram Jaccard + 別名精確命中。
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
DB = "/Users/jen/ai_work_space/related_to_claw/aka_no_claw/data/knowledge.sqlite3"
OLLAMA = "http://localhost:11434/api/embeddings"
K = 3

# model -> (doc_prefix, query_prefix). nomic 要求前綴；bge-m3 不需要。
MODELS = {
    "nomic-embed-text": ("search_document: ", "search_query: "),
    "bge-m3": ("", ""),
}


# ── corpus ───────────────────────────────────────────────────────────────────

def load_corpus() -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT entity_canonical, COALESCE(summary,'') FROM knowledge_entries"
        ).fetchall()
        aliases: dict[str, list[str]] = {}
        for alias, canon in con.execute(
            "SELECT alias, entity_canonical FROM entity_aliases"
        ):
            aliases.setdefault(canon, []).append(alias)
    finally:
        con.close()
    corpus = []
    for canon, summary in rows:
        al = aliases.get(canon, [])
        text = canon + " | " + " ; ".join(al) + " | " + summary
        corpus.append(
            {"canon": canon, "aliases": al, "summary": summary, "text": text}
        )
    return corpus


# ── lexical baseline (char-bigram Jaccard + exact-alias short-circuit) ────────

def bigrams(s: str) -> set[str]:
    s = "".join(s.lower().split())
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}


def lexical_rank(query: str, corpus: list[dict]) -> list[tuple[str, float]]:
    q_norm = "".join(query.lower().split())
    qb = bigrams(query)
    scored = []
    for e in corpus:
        exact = 0.0
        for a in [e["canon"], *e["aliases"]]:
            an = "".join(a.lower().split())
            if an and (an in q_norm or q_norm in an):
                exact = max(exact, 1.0 + len(an) / 50.0)
        tb = bigrams(e["text"])
        jac = len(qb & tb) / len(qb | tb) if (qb | tb) else 0.0
        scored.append((e["canon"], exact * 10 + jac))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


# ── embedding retriever ──────────────────────────────────────────────────────

def embed(text: str, model: str) -> np.ndarray:
    body = json.dumps({"model": model, "prompt": text}).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        v = np.asarray(json.loads(r.read())["embedding"], dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else v


def build_embeddings(corpus: list[dict], model: str, doc_prefix: str) -> np.ndarray:
    cache = HERE / f"emb_{model.replace(':','_')}.npz"
    if cache.exists():
        d = np.load(cache, allow_pickle=True)
        if list(d["canon"]) == [e["canon"] for e in corpus]:
            return d["mat"]
    mat = np.vstack([embed(doc_prefix + e["text"], model) for e in corpus]).astype(np.float32)
    np.savez(cache, mat=mat, canon=np.array([e["canon"] for e in corpus], dtype=object))
    return mat


def embed_rank(qv: np.ndarray, mat: np.ndarray, corpus: list[dict]) -> list[tuple[str, float]]:
    sims = mat @ qv
    order = np.argsort(-sims)
    return [(corpus[i]["canon"], float(sims[i])) for i in order]


# ── metrics ──────────────────────────────────────────────────────────────────

def rank_of(target: str, ranked: list[tuple[str, float]]) -> int | None:
    for i, (canon, _) in enumerate(ranked):
        if canon == target:
            return i + 1
    return None


def eval_ranker(cases, rank_fn) -> dict:
    r1 = r3 = 0
    mrr = 0.0
    lat = []
    per = []
    for c in cases:
        t = time.time()
        ranked = rank_fn(c["query"])
        lat.append((time.time() - t) * 1000)
        rk = rank_of(c["target"], ranked)
        if rk == 1:
            r1 += 1
        if rk and rk <= 3:
            r3 += 1
        if rk:
            mrr += 1 / rk
        per.append({"id": c["id"], "rank": rk, "top1": ranked[0][0]})
    n = len(cases)
    return {"hit1": r1, "hit3": r3, "mrr": mrr / n, "lat_ms": float(np.mean(lat)), "per": per}


def main() -> None:
    cases = json.loads((HERE / "cases.json").read_text())
    corpus = load_corpus()
    canons = {e["canon"] for e in corpus}
    for c in cases:
        assert c["target"] in canons, f"target not in KB: {c['target']}"

    available = _ollama_models()
    results = {"lexical": eval_ranker(cases, lambda q: lexical_rank(q, corpus))}
    meta = {}
    for model, (dprefix, qprefix) in MODELS.items():
        if model not in available:
            print(f"[skip] {model} not pulled")
            continue
        t0 = time.time()
        mat = build_embeddings(corpus, model, dprefix)
        build_s = time.time() - t0
        meta[model] = {"build_s": build_s, "dim": int(mat.shape[1]), "mem_kb": mat.nbytes / 1024}
        results[model] = eval_ranker(
            cases, lambda q, m=model, mt=mat, p=qprefix: embed_rank(embed(p + q, m), mt, corpus)
        )

    write_md(cases, corpus, results, meta)
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "per"} for k, v in results.items()},
                     ensure_ascii=False, indent=2))


def _ollama_models() -> set[str]:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=10) as r:
            tags = json.loads(r.read())["models"]
        return {m["name"].split(":")[0] if ":" not in m["name"] else m["name"] for m in tags} | {
            m["name"].split(":")[0] for m in tags
        }
    except Exception:
        return set()


def short(s: str, n: int = 38) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def write_md(cases, corpus, results, meta) -> None:
    cmap = {c["id"]: c for c in cases}
    n = len(cases)
    out = []
    out.append("# Embedding 檢索 spike — 修改前後效能比較\n")
    out.append(
        f"資料：龍蝦知識庫 `knowledge.sqlite3`（{n} 題測試 / {len(corpus)} entries，含中日英別名）。\n"
        "本 spike 只讀庫、未接 production，可隨時 `rm -rf spikes/embedding_retrieval/` 拔掉。\n"
    )
    out.append("## 總分（lexical = 現況；embedding = 提案）\n")
    cols = list(results.keys())
    out.append("| 指標 | " + " | ".join(cols) + " |")
    out.append("|---|" + "---|" * len(cols))
    out.append("| hit@1 | " + " | ".join(f"{results[c]['hit1']}/{n}" for c in cols) + " |")
    out.append("| hit@3 | " + " | ".join(f"{results[c]['hit3']}/{n}" for c in cols) + " |")
    out.append("| MRR | " + " | ".join(f"{results[c]['mrr']:.3f}" for c in cols) + " |")
    out.append("| 查詢延遲 | " + " | ".join(f"{results[c]['lat_ms']:.1f}ms" for c in cols) + " |")
    out.append("")
    for m, mm in meta.items():
        out.append(f"- `{m}`：維度 {mm['dim']}，建索引 {mm['build_s']:.1f}s，向量常駐 {mm['mem_kb']:.0f} KB")
    out.append("")
    out.append("## 逐題名次（數字=正解排第幾名，— = 前 149 名外）\n")
    head = "| # | 類型 | 查詢 | 正解 | " + " | ".join(cols) + " |"
    out.append(head)
    out.append("|---|---|---|---|" + "---|" * len(cols))
    for c in cases:
        cid = c["id"]
        cells = []
        for col in cols:
            per = next(p for p in results[col]["per"] if p["id"] == cid)
            mark = str(per["rank"]) if per["rank"] else "—"
            if per["rank"] != 1:
                mark += f"（top1={short(per['top1'],14)}）"
            cells.append(mark)
        out.append(
            f"| {cid} | {c['kind']} | {short(c['query'],20)} | {short(c['target'],16)} | "
            + " | ".join(cells)
            + " |"
        )
    out.append("")
    (HERE / "RESULTS.md").write_text("\n".join(out), encoding="utf-8")


if __name__ == "__main__":
    main()
