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


def _install_semantic_title_matcher() -> None:
    """Register the bge-m3 semantic title matcher onto the Mercari search filter.

    Best-effort: if settings/embedder/Ollama are unavailable the Mercari search
    keeps its lexical token filter, so a scrape never fails just because the
    embedder is down — it only loses the recall improvement."""
    try:
        from assistant_runtime import build_ssl_context, get_settings, load_dotenv
        from market_monitor.mercari_search import _lexical_filter_by_query, set_title_matcher

        from .kb_embedder import build_kb_embedder
        from .title_match import build_semantic_title_matcher

        load_dotenv()
        settings = get_settings()
        embedder = build_kb_embedder(settings, ssl_context=build_ssl_context(settings))
        if embedder is None:
            logging.getLogger(__name__).info(
                "scrape_worker: embedder unavailable — Mercari filter stays lexical"
            )
            return
        matcher = build_semantic_title_matcher(
            embedder,
            threshold=settings.openclaw_research_title_match_threshold,
            lexical_fallback=lambda query, items: _lexical_filter_by_query(items, query),
        )
        set_title_matcher(matcher)
        logging.getLogger(__name__).info(
            "scrape_worker: semantic title matcher installed (threshold=%.2f)",
            settings.openclaw_research_title_match_threshold,
        )
    except Exception:  # noqa: BLE001 - never let matcher wiring break a scrape
        logging.getLogger(__name__).warning(
            "scrape_worker: failed to install semantic title matcher; staying lexical",
            exc_info=True,
        )


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
        if ref is None:
            return None
        if isinstance(ref, dict):  # pre-network budget-skip sentinel — pass through
            return ref
        return rc._shop_reference_to_dict(ref)
    raise ValueError(f"unknown scrape target {target!r}")


def main() -> None:
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr, force=True)
    _install_semantic_title_matcher()
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
