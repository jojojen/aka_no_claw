#!/usr/bin/env python3
"""Local Ollama native tool-calling benchmark.

This is intentionally self-contained and deterministic. The goal is not to
measure web quality; it is to verify whether a local model can reliably choose
tools, pass usable arguments, consume tool results, and stop with a final answer.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_MODELS = ("qwen3:4b", "qwen2.5-coder:7b", "qwen3:14b")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Return deterministic current weather for a city.",
            "parameters": {
                "type": "object",
                "required": ["city"],
                "properties": {
                    "city": {"type": "string", "description": "City name, e.g. 東京"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_music",
            "description": "Search recent popular songs for an artist.",
            "parameters": {
                "type": "object",
                "required": ["artist"],
                "properties": {
                    "artist": {"type": "string", "description": "Artist name"},
                    "limit": {"type": "integer", "description": "Number of songs"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_song_detail",
            "description": "Return deterministic release metadata for one song title.",
            "parameters": {
                "type": "object",
                "required": ["title"],
                "properties": {
                    "title": {"type": "string", "description": "Song title"}
                },
            },
        },
    },
]


def get_weather(city: str) -> dict[str, str]:
    return {"city": city, "condition": "晴れ", "temperature_c": "28", "source": "fixture"}


def search_music(artist: str, limit: int = 3) -> dict[str, Any]:
    songs = [
        {"title": "Lemon", "rank": 1},
        {"title": "KICK BACK", "rank": 2},
        {"title": "感電", "rank": 3},
    ]
    return {"artist": artist, "songs": songs[: max(1, min(int(limit or 3), 3))], "source": "fixture"}


def get_song_detail(title: str) -> dict[str, str]:
    details = {
        "Lemon": {"title": "Lemon", "released": "2018-03-14", "tie_in": "Unnatural theme song"},
        "KICK BACK": {"title": "KICK BACK", "released": "2022-10-12", "tie_in": "Chainsaw Man opening"},
        "感電": {"title": "感電", "released": "2020-07-06", "tie_in": "MIU404 theme song"},
    }
    return details.get(title, {"title": title, "released": "unknown", "tie_in": "unknown"})


TOOL_IMPLS = {
    "get_weather": get_weather,
    "search_music": search_music,
    "get_song_detail": get_song_detail,
}


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
    normalized = []
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
            normalized.append({"name": name, "arguments": args if isinstance(args, dict) else {}})
    return normalized


def missing_expected_tools(expected: list[str], observed: list[str]) -> list[str]:
    missing: list[str] = []
    cursor = 0
    for name in expected:
        try:
            found_at = observed.index(name, cursor)
        except ValueError:
            missing.append(name)
        else:
            cursor = found_at + 1
    return missing


def run_case(
    endpoint: str,
    model: str,
    case: dict[str, Any],
    timeout: int,
    max_rounds: int,
    subgoal_gate: bool,
) -> dict[str, Any]:
    started = time.time()
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "你正在一個工具調用測試迴圈中。需要外部資料時請呼叫工具；"
                "工具結果回來後再用繁體中文給最終答案。若不需要工具，請直接回答。"
            ),
        }
    ]
    messages.extend(case.get("history") or [])
    messages.append({"role": "user", "content": case["prompt"]})

    observed_tools: list[dict[str, Any]] = []
    raw_rounds: list[dict[str, Any]] = []
    final_content = ""
    error = None
    gate_interventions: list[dict[str, Any]] = []
    expected = case.get("expected_tools") or []

    for round_index in range(max_rounds):
        payload = {
            "model": model,
            "messages": messages,
            "tools": TOOLS,
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "num_predict": 512},
        }
        try:
            response = post_json(f"{endpoint.rstrip('/')}/api/chat", payload, timeout)
        except (HTTPError, URLError, TimeoutError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            break
        message = response.get("message") or {}
        if not isinstance(message, dict):
            error = f"bad message type: {type(message).__name__}"
            break
        calls = normalize_tool_calls(message)
        raw_rounds.append(
            {
                "round": round_index + 1,
                "content": message.get("content", ""),
                "thinking_present": bool(message.get("thinking")),
                "tool_calls": calls,
            }
        )
        messages.append(message)
        if not calls:
            final_content = str(message.get("content") or "").strip()
            missing = missing_expected_tools(expected, [item["name"] for item in observed_tools])
            if subgoal_gate and missing and round_index < max_rounds - 1:
                next_tool = missing[0]
                correction = (
                    f"你尚未完成必要工具 `{next_tool}`。"
                    "不要編造缺少的資料，請先呼叫下一個必要工具；"
                    "工具結果回來後再給最終答案。"
                )
                gate_interventions.append(
                    {
                        "after_round": round_index + 1,
                        "missing_tools": missing,
                        "message": correction,
                    }
                )
                messages.append({"role": "user", "content": correction})
                final_content = ""
                continue
            break
        for call in calls:
            name = call["name"]
            args = call["arguments"]
            impl = TOOL_IMPLS.get(name)
            if impl is None:
                result: Any = {"error": f"unknown tool: {name}"}
            else:
                try:
                    result = impl(**args)
                except Exception as exc:  # noqa: BLE001
                    result = {"error": f"{type(exc).__name__}: {exc}", "arguments": args}
            observed_tools.append({"name": name, "arguments": args, "result": result})
            messages.append(
                {
                    "role": "tool",
                    "tool_name": name,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    observed_names = [item["name"] for item in observed_tools]
    stripped_final = final_content.strip()
    leaked_thinking = "</think>" in stripped_final or "<think>" in stripped_final
    raw_json_tool_text = bool(re.match(r'^\s*\{\s*"name"\s*:', stripped_final))
    quality_flags = {
        "leaked_thinking": leaked_thinking,
        "raw_json_tool_text": raw_json_tool_text,
    }
    passed = (
        error is None
        and expected == observed_names[: len(expected)]
        and bool(final_content or not expected)
        and not any(quality_flags.values())
    )
    if not expected:
        passed = (
            error is None
            and not observed_tools
            and bool(final_content)
            and not any(quality_flags.values())
        )
    return {
        "case_id": case["id"],
        "model": model,
        "passed": passed,
        "expected_tools": expected,
        "observed_tools": observed_tools,
        "final_content": final_content,
        "quality_flags": quality_flags,
        "gate_interventions": gate_interventions,
        "rounds": raw_rounds,
        "duration_seconds": round(time.time() - started, 3),
        "error": error,
    }


def write_markdown(summary_path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Local Tool Calling Benchmark Results",
        "",
        "Last reviewed: 2026-06-24",
        "Status: Generated",
        "Owner area: agent-maintenance",
        "",
        f"- Timestamp: `{payload['timestamp']}`",
        f"- Endpoint: `{payload['endpoint']}`",
        f"- Models: `{', '.join(payload['models'])}`",
        "",
        "| Model | Passed | Total | Pass rate | Mean seconds |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for model, stats in payload["summary"].items():
        lines.append(
            f"| `{model}` | {stats['passed']} | {stats['total']} | "
            f"{stats['pass_rate']:.0%} | {stats['mean_seconds']:.1f} |"
        )
    lines.extend(["", "## Case Details", ""])
    for result in payload["results"]:
        status = "PASS" if result["passed"] else "FAIL"
        lines.append(f"### {result['model']} / {result['case_id']} / {status}")
        lines.append(f"- Duration: `{result['duration_seconds']}s`")
        lines.append(f"- Expected tools: `{result['expected_tools']}`")
        lines.append(f"- Observed tools: `{[t['name'] for t in result['observed_tools']]}`")
        if result["error"]:
            lines.append(f"- Error: `{result['error']}`")
        active_flags = [name for name, active in result.get("quality_flags", {}).items() if active]
        if active_flags:
            lines.append(f"- Quality flags: `{active_flags}`")
        if result.get("gate_interventions"):
            lines.append(f"- Gate interventions: `{len(result['gate_interventions'])}`")
        if result["final_content"]:
            lines.append("- Final content:")
            lines.append("")
            lines.append("```text")
            lines.append(result["final_content"])
            lines.append("```")
        lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--cases", default=str(ROOT / "cases.json"))
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument(
        "--subgoal-gate",
        action="store_true",
        help="Prompt the model to continue when expected tools are still missing.",
    )
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    results = []
    for model in args.models:
        for case in cases:
            print(f"running {model} / {case['id']}...", flush=True)
            results.append(
                run_case(
                    args.endpoint,
                    model,
                    case,
                    args.timeout,
                    args.max_rounds,
                    args.subgoal_gate,
                )
            )

    summary: dict[str, dict[str, Any]] = {}
    for model in args.models:
        rows = [r for r in results if r["model"] == model]
        passed = sum(1 for r in rows if r["passed"])
        total = len(rows)
        summary[model] = {
            "passed": passed,
            "total": total,
            "pass_rate": passed / total if total else 0.0,
            "mean_seconds": sum(r["duration_seconds"] for r in rows) / total if total else 0.0,
        }

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "endpoint": args.endpoint,
        "models": args.models,
        "subgoal_gate": args.subgoal_gate,
        "summary": summary,
        "results": results,
    }
    out_json = ROOT / "latest_results.json"
    out_md = ROOT / "latest_results.md"
    archive_json = ROOT / f"results_{timestamp}.json"
    archive_md = ROOT / f"results_{timestamp}.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(out_md, payload)
    archive_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(archive_md, payload)
    print(f"wrote {out_json}")
    print(f"wrote {out_md}")
    print(f"wrote {archive_json}")
    print(f"wrote {archive_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
