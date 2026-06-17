"""Subprocess entrypoint for isolated Playwright research scrapes.

Reads one JSON request ``{"target": str, "payload": dict}`` on stdin, dispatches
to the matching raw scrape implementation in :mod:`openclaw_adapter.research_command`,
and writes ``{"ok": bool, "result"|"error": ...}`` JSON on stdout. All logging
goes to stderr so the parent (run_in_subprocess) can forward it without
corrupting the JSON result on stdout.

Run as: ``python -m openclaw_adapter.scrape_worker``.
"""

from __future__ import annotations

import json
import logging
import sys


def _dispatch(target: str, payload: dict) -> object:
    from openclaw_adapter import research_command as rc

    if target == "active":
        return rc._active_market_scrape_impl(
            payload["query"], int(payload["price_cap"]), int(payload["max_results"])
        )
    if target == "sold":
        return rc._sold_market_scrape_impl(payload["query"], int(payload["max_results"]))
    if target == "sold_avg":
        return rc._sold_average_scrape_impl(payload["query"])
    if target == "shop_reference":
        ref = rc._shop_reference_scrape_impl(
            payload["query"], int(payload["price_cap"]), payload.get("source_options")
        )
        return None if ref is None else rc._shop_reference_to_dict(ref)
    raise ValueError(f"unknown scrape target {target!r}")


def main() -> None:
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr, force=True)
    raw = sys.stdin.read()
    try:
        request = json.loads(raw)
        result = _dispatch(request["target"], request.get("payload") or {})
        payload = {"ok": True, "result": result}
    except BaseException as exc:  # noqa: BLE001 — report any failure as JSON
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
