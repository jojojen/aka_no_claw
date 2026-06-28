#!/usr/bin/env python3
"""Semi-real local tool-calling probe for the music-search workflow.

This does not play audio. It uses the real OpenClaw music index and, when
enabled, the real web_search backend, then asks the local model to pick a local
song. It is meant to validate feasibility before touching production chat code.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from assistant_runtime import AssistantSettings
from openclaw_adapter.music_command import _search, load_or_build_index
from openclaw_adapter.web_search import web_search


ROOT = Path(__file__).resolve().parent
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"


def post_json(url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    req = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def normalize_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls") or []
    out = []
    for call in calls:
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"_raw": args}
        if name:
            out.append({"name": name, "arguments": args if isinstance(args, dict) else {}})
    return out


def build_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_local_music",
                "description": "List local indexed music filenames matching an artist or keyword. Does not play audio.",
                "parameters": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_search_top_songs",
                "description": "Search the web for recent popular songs by an artist.",
                "parameters": {
                    "type": "object",
                    "required": ["artist"],
                    "properties": {"artist": {"type": "string"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "select_local_song",
                "description": "Select one local indexed song by title or keyword. Does not play audio.",
                "parameters": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {"title": {"type": "string"}},
                },
            },
        },
    ]


class RealisticTools:
    def __init__(self, *, live_web: bool) -> None:
        self.settings = AssistantSettings()
        self.music_root = Path(self.settings.openclaw_music_dir).resolve()
        self.index = load_or_build_index(
            self.settings.openclaw_music_dir,
            self.settings.openclaw_music_index_path,
        )
        self.live_web = live_web

    def display_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        path = str(entry.get("path") or "")
        try:
            rel = Path(path).resolve().relative_to(self.music_root)
            safe_path = f"<music_root>/{rel.as_posix()}"
        except (OSError, ValueError):
            safe_path = "<music_root>/<outside-index>"
        return {"name": entry.get("name"), "path": safe_path}

    def list_local_music(self, query: str) -> dict[str, Any]:
        q = str(query or "").casefold()
        matches = [
            self.display_entry(e)
            for e in self.index.entries
            if q in str(e.get("name") or "").casefold()
            or q in str(e.get("path") or "").casefold()
        ][:20]
        return {"count": len(matches), "matches": matches}

    def web_search_top_songs(self, artist: str) -> dict[str, Any]:
        query = f"{artist} 人気曲 ランキング 最新"
        if not self.live_web:
            return {
                "query": query,
                "results": [
                    {"title": "米津玄師 人気曲 ランキング", "snippet": "Lemon, KICK BACK, LOSER, ピースサインなど。"}
                ],
                "live": False,
            }
        rows = web_search(query, max_results=3, reuse_browser=False)
        return {
            "query": query,
            "results": [
                {"title": r.title, "url": r.url, "snippet": r.snippet}
                for r in rows
            ],
            "live": True,
        }

    def select_local_song(self, title: str) -> dict[str, Any]:
        result = _search(self.index.entries, str(title or ""))
        if result.kind in {"exact", "single"} and result.entry:
            return {"kind": result.kind, "selected": self.display_entry(result.entry)}
        if result.kind == "ambiguous":
            return {
                "kind": result.kind,
                "candidates": [self.display_entry(e) for e in result.candidates],
            }
        return {"kind": "none", "title": title}

    def call(self, name: str, args: dict[str, Any]) -> Any:
        fn = getattr(self, name, None)
        if not callable(fn):
            return {"error": f"unknown tool: {name}"}
        try:
            return fn(**args)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}", "arguments": args}


def missing(expected: list[str], observed: list[str]) -> list[str]:
    cursor = 0
    out = []
    for name in expected:
        try:
            found = observed.index(name, cursor)
        except ValueError:
            out.append(name)
        else:
            cursor = found + 1
    return out


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    tools = RealisticTools(live_web=args.live_web)
    expected = ["list_local_music", "web_search_top_songs", "select_local_song"]
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "你正在測試本機工具規劃。請依序先列出本機音樂、再查網路熱門歌、"
                "再選一首本機存在的歌。禁止播放音訊；只回報會選哪首。"
            ),
        },
        {
            "role": "user",
            "content": "請選出一首本機有的米津玄師熱門單曲。不要播放，只回傳將選擇的歌曲名與理由。",
        },
    ]
    observed: list[dict[str, Any]] = []
    rounds: list[dict[str, Any]] = []
    final = ""
    started = time.time()

    for i in range(args.max_rounds):
        response = post_json(
            f"{args.endpoint.rstrip('/')}/api/chat",
            {
                "model": args.model,
                "messages": messages,
                "tools": build_tools(),
                "stream": False,
                "think": False,
                "options": {"temperature": 0, "num_predict": 700},
            },
            args.timeout,
        )
        message = response.get("message") or {}
        calls = normalize_tool_calls(message)
        rounds.append({"round": i + 1, "content": message.get("content", ""), "tool_calls": calls})
        messages.append(message)
        if not calls:
            final = str(message.get("content") or "").strip()
            miss = missing(expected, [x["name"] for x in observed])
            if miss and i < args.max_rounds - 1:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"仍缺少必要工具 `{miss[0]}`。請先呼叫它，"
                            "不要根據猜測或未驗證資料完成。"
                        ),
                    }
                )
                final = ""
                continue
            break
        for call in calls:
            result = tools.call(call["name"], call["arguments"])
            observed.append({"name": call["name"], "arguments": call["arguments"], "result": result})
            messages.append(
                {
                    "role": "tool",
                    "tool_name": call["name"],
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    observed_names = [x["name"] for x in observed]
    passed = missing(expected, observed_names) == [] and bool(final)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "live_web": args.live_web,
        "expected_tools": expected,
        "observed_tools": observed,
        "passed": passed,
        "final_content": final,
        "rounds": rounds,
        "duration_seconds": round(time.time() - started, 3),
        "music_entries": len(tools.index.entries),
    }


def write_md(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Realistic Local Tool Probe",
        "",
        "Last reviewed: 2026-06-24",
        "Status: Generated",
        "Owner area: agent-maintenance",
        "",
        f"- Timestamp: `{payload['timestamp']}`",
        f"- Model: `{payload['model']}`",
        f"- Live web: `{payload['live_web']}`",
        f"- Music entries: `{payload['music_entries']}`",
        f"- Passed: `{payload['passed']}`",
        f"- Duration: `{payload['duration_seconds']}s`",
        f"- Expected tools: `{payload['expected_tools']}`",
        f"- Observed tools: `{[x['name'] for x in payload['observed_tools']]}`",
        "",
        "## Final Content",
        "",
        "```text",
        payload.get("final_content") or "",
        "```",
        "",
        "## Tool Calls",
        "",
    ]
    for item in payload["observed_tools"]:
        lines.append(f"### {item['name']}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(item, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default="qwen3:14b")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-rounds", type=int, default=8)
    parser.add_argument("--live-web", action="store_true")
    args = parser.parse_args()

    payload = run_probe(args)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_json = ROOT / f"realistic_probe_{stamp}.json"
    out_md = ROOT / f"realistic_probe_{stamp}.md"
    latest_json = ROOT / "latest_realistic_probe.json"
    latest_md = ROOT / "latest_realistic_probe.md"
    for path in (out_json, latest_json):
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for path in (out_md, latest_md):
        write_md(path, payload)
    print(f"wrote {out_json}")
    print(f"wrote {out_md}")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
