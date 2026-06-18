from __future__ import annotations

import argparse
import json
import logging
import threading
import time
import webbrowser
from dataclasses import replace
from pathlib import Path

from assistant_runtime import AssistantSettings, AssistantTool, ToolRegistry, build_ssl_context, get_settings
from tcg_tracker.catalog import SUPPORTED_GAMES, normalize_game_key

from .commands import list_reference_sources, lookup_card, seed_example_watchlist
from .dashboard import serve_dashboard
from .formatters import (
    format_lookup_result,
    format_reference_sources,
    lookup_result_to_json,
    reference_sources_to_json,
)
from .opportunity_agent import format_opportunity_status, run_opportunity_agent
from .reputation_agent import check_prerequisites, ensure_agent_thread, run_agent_loop
from .sns_tools import (
    _configure_sns_add_account_parser,
    _configure_sns_add_keyword_parser,
    _configure_sns_add_trend_parser,
    _configure_sns_delete_parser,
    _configure_sns_list_parser,
    _configure_sns_toggle_parser,
    _handle_sns_add_account,
    _handle_sns_add_keyword,
    _handle_sns_add_trend,
    _handle_sns_delete,
    _handle_sns_list,
    _handle_sns_toggle,
)
from .telegram_bot import (
    default_board_loader,
    default_lookup_renderer,
    _select_text_generation_model,
    run_telegram_polling,
    send_telegram_test_message,
)
from .web_search import (
    build_web_research_answer,
    fetch_page_text,
    filter_relevant_sources_with_ollama,
    format_web_research_answer,
    reformulate_queries_with_ollama,
    summarize_web_sources_with_ollama,
    web_search,
)

logger = logging.getLogger(__name__)


def build_tool_registry(settings: AssistantSettings | None = None) -> ToolRegistry:
    settings = settings or get_settings()
    registry = ToolRegistry()
    registry.register(
        AssistantTool(
            name="tcg.lookup-card",
            description="Look up a TCG card price profile via the current price modules.",
            configure_parser=lambda parser: _configure_lookup_card_parser(parser, settings),
            handler=_handle_lookup_card,
            aliases=("lookup-card",),
        )
    )
    registry.register(
        AssistantTool(
            name="tcg.seed-example-watchlist",
            description="Seed the example TCG watchlist into SQLite.",
            configure_parser=lambda parser: _configure_seed_watchlist_parser(parser, settings),
            handler=_handle_seed_watchlist,
            aliases=("seed-example-watchlist",),
        )
    )
    registry.register(
        AssistantTool(
            name="market.list-reference-sources",
            description="List reference sources available to pricing and monitoring modules.",
            configure_parser=_configure_reference_sources_parser,
            handler=_handle_list_reference_sources,
            aliases=("list-reference-sources",),
        )
    )
    registry.register(
        AssistantTool(
            name="assistant.serve-dashboard",
            description="Run a local dashboard UI for inspecting the assistant workspace.",
            configure_parser=_configure_dashboard_parser,
            handler=lambda args: _handle_serve_dashboard(args, settings, registry),
            aliases=("serve-dashboard",),
        )
    )
    registry.register(
        AssistantTool(
            name="assistant.telegram-poll",
            description="Run the Telegram long-polling test bot using the configured .env credentials.",
            configure_parser=_configure_telegram_poll_parser,
            handler=lambda args: _handle_telegram_poll(args, settings, registry),
            aliases=("telegram-poll",),
        )
    )
    registry.register(
        AssistantTool(
            name="assistant.telegram-send-test",
            description="Send a test message to the configured Telegram chat.",
            configure_parser=_configure_telegram_send_test_parser,
            handler=lambda args: _handle_telegram_send_test(args, settings),
            aliases=("telegram-send-test",),
        )
    )
    registry.register(
        AssistantTool(
            name="assistant.web-search",
            description="Search the web with Yahoo Japan and summarize the sources with the configured local LLM.",
            configure_parser=_configure_web_search_parser,
            handler=lambda args: _handle_web_search(args, settings),
            aliases=("web-search", "research", "search-web"),
        )
    )
    registry.register(
        AssistantTool(
            name="assistant.reputation-agent",
            description="Run the reputation-snapshot polling agent (claims jobs from the server and executes Playwright captures).",
            configure_parser=lambda parser: _configure_reputation_agent_parser(parser, settings),
            handler=lambda args: _handle_reputation_agent(args, settings),
            aliases=("reputation-agent",),
        )
    )
    registry.register(
        AssistantTool(
            name="assistant.opportunity-agent",
            description="Run the SNS→price→reputation opportunity pipeline and recommend qualified Mercari listings via Telegram.",
            configure_parser=lambda parser: _configure_opportunity_agent_parser(parser, settings),
            handler=lambda args: _handle_opportunity_agent(args, settings),
            aliases=("opportunity-agent", "hunt-agent"),
        )
    )
    registry.register(
        AssistantTool(
            name="assistant.sns-monitor-service",
            description="Run the SNS background monitor service (RSS polling, classifier, push notifications, inbox processing). Intended for local.openclaw.sns_monitor launchd.",
            configure_parser=lambda parser: None,
            handler=lambda args: _handle_sns_monitor_service(args, settings),
            aliases=("sns-monitor-service",),
        )
    )
    registry.register(
        AssistantTool(
            name="assistant.price-monitor-service",
            description="Run the price monitor background service (watch_monitor, card image crawler, watch_inbox processing). Intended for local.openclaw.price_monitor launchd.",
            configure_parser=lambda parser: None,
            handler=lambda args: _handle_price_monitor_service(args, settings),
            aliases=("price-monitor-service",),
        )
    )
    registry.register(
        AssistantTool(
            name="assistant.opportunity-status",
            description="Show recent opportunity candidates and recommendation decisions.",
            configure_parser=_configure_opportunity_status_parser,
            handler=lambda args: _handle_opportunity_status(args, settings),
            aliases=("opportunity-status", "hunt-status"),
        )
    )
    registry.register(
        AssistantTool(
            name="sns.add-account",
            description="Add an X (Twitter) account to the SNS watch list.",
            configure_parser=lambda parser: _configure_sns_add_account_parser(parser, settings),
            handler=lambda args: _handle_sns_add_account(args, settings),
            aliases=("sns-add-account",),
        )
    )
    registry.register(
        AssistantTool(
            name="sns.add-keyword",
            description="Add a keyword search to the SNS watch list.",
            configure_parser=lambda parser: _configure_sns_add_keyword_parser(parser, settings),
            handler=lambda args: _handle_sns_add_keyword(args, settings),
            aliases=("sns-add-keyword",),
        )
    )
    registry.register(
        AssistantTool(
            name="sns.add-trend",
            description="Add a trend category to the SNS watch list.",
            configure_parser=lambda parser: _configure_sns_add_trend_parser(parser, settings),
            handler=lambda args: _handle_sns_add_trend(args, settings),
            aliases=("sns-add-trend",),
        )
    )
    registry.register(
        AssistantTool(
            name="sns.list-rules",
            description="List all SNS watch rules.",
            configure_parser=lambda parser: _configure_sns_list_parser(parser, settings),
            handler=lambda args: _handle_sns_list(args, settings),
            aliases=("sns-list-rules", "sns-list"),
        )
    )
    registry.register(
        AssistantTool(
            name="sns.toggle-rule",
            description="Enable or disable a SNS watch rule.",
            configure_parser=lambda parser: _configure_sns_toggle_parser(parser, settings),
            handler=lambda args: _handle_sns_toggle(args, settings),
            aliases=("sns-toggle-rule",),
        )
    )
    registry.register(
        AssistantTool(
            name="sns.delete-rule",
            description="Delete a SNS watch rule.",
            configure_parser=lambda parser: _configure_sns_delete_parser(parser, settings),
            handler=lambda args: _handle_sns_delete(args, settings),
            aliases=("sns-delete-rule",),
        )
    )
    return registry


def render_tool_catalog(registry: ToolRegistry) -> str:
    lines = ["OpenClaw assistant tools:"]
    for tool in registry.tools():
        alias_text = f" | aliases: {', '.join(tool.aliases)}" if tool.aliases else ""
        lines.append(f"- {tool.name}: {tool.description}{alias_text}")
    return "\n".join(lines)


def _configure_lookup_card_parser(parser: argparse.ArgumentParser, settings: AssistantSettings) -> None:
    parser.add_argument("--db", default=settings.monitor_db_path)
    parser.add_argument("--game", choices=[*SUPPORTED_GAMES, "ygo", "ua"], required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--card-number")
    parser.add_argument("--rarity")
    parser.add_argument("--set-code")
    parser.add_argument("--set-name")
    parser.add_argument("--alias", action="append", default=[])
    parser.add_argument("--keyword", action="append", default=[])
    parser.add_argument("--json", action="store_true")


def _configure_seed_watchlist_parser(parser: argparse.ArgumentParser, settings: AssistantSettings) -> None:
    parser.add_argument("--db", default=settings.monitor_db_path)
    parser.add_argument("--config", default="config/example_watchlist.json")


def _configure_reference_sources_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="config/reference_sources.json")
    parser.add_argument("--game", choices=[*SUPPORTED_GAMES, "ygo", "ua"])
    parser.add_argument("--kind")
    parser.add_argument("--role")
    parser.add_argument("--json", action="store_true")


def _configure_dashboard_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--with-reputation-agent", action="store_true",
                        help="Also start the reputation-snapshot polling agent in a background thread.")


def _configure_telegram_poll_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--poll-timeout", type=int, default=20)
    parser.add_argument("--notify-startup", action="store_true")
    parser.add_argument("--keep-pending", action="store_true")
    parser.add_argument("--with-reputation-agent", action="store_true",
                        help="Also start the reputation-snapshot polling agent in a background thread.")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Do not auto-start the dashboard server and open browser.")
    parser.add_argument("--dashboard-port", type=int, default=8765,
                        help="Port for the auto-started dashboard (default: 8765).")


def _configure_telegram_send_test_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--message", default="OpenClaw Telegram test successful.")


def _configure_web_search_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query", nargs="+", help="Question or search query to research.")
    parser.add_argument("--provider", choices=["yahoo_japan"], default="yahoo_japan")
    parser.add_argument("--limit", type=int, default=5, help="Number of search results to use, 1-10.")
    parser.add_argument("--json", action="store_true", help="Print structured answer JSON.")


def _handle_lookup_card(args: argparse.Namespace) -> int:
    logger.info(
        "CLI lookup command received game=%s name=%s card_number=%s rarity=%s set_code=%s json=%s",
        args.game,
        args.name,
        args.card_number,
        args.rarity,
        args.set_code,
        args.json,
    )
    result = lookup_card(
        db_path=args.db,
        game=normalize_game_key(args.game) or args.game,
        name=args.name,
        card_number=args.card_number,
        rarity=args.rarity,
        set_code=args.set_code,
        set_name=args.set_name,
        aliases=tuple(args.alias),
        extra_keywords=tuple(args.keyword),
    )
    print(lookup_result_to_json(result) if args.json else format_lookup_result(result))
    return 0


def _handle_seed_watchlist(args: argparse.Namespace) -> int:
    logger.info("CLI seed watchlist command received db=%s config=%s", args.db, args.config)
    inserted = seed_example_watchlist(db_path=Path(args.db), config_path=Path(args.config))
    print(f"seeded {inserted} example watchlist items into {args.db}")
    return 0


def _handle_list_reference_sources(args: argparse.Namespace) -> int:
    logger.info(
        "CLI list-reference-sources command received config=%s game=%s kind=%s role=%s json=%s",
        args.config,
        args.game,
        args.kind,
        args.role,
        args.json,
    )
    sources = list_reference_sources(
        config_path=Path(args.config),
        game=normalize_game_key(args.game) if args.game else None,
        source_kind=args.kind,
        reference_role=args.role,
    )
    print(reference_sources_to_json(sources) if args.json else format_reference_sources(sources))
    return 0


def _handle_web_search(args: argparse.Namespace, settings: AssistantSettings) -> int:
    query = " ".join(args.query).strip()
    model = _select_text_generation_model(settings)
    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    endpoint = settings.openclaw_local_text_endpoint
    if backend != "ollama" or not endpoint or not model:
        print("ERROR: configure OPENCLAW_LOCAL_TEXT_BACKEND=ollama and OPENCLAW_LOCAL_TEXT_MODEL to summarize web results.")
        return 1

    ssl_ctx = build_ssl_context(settings) if endpoint.startswith("https://") else None
    timeout = max(1, settings.openclaw_local_text_timeout_seconds)
    answer = build_web_research_answer(
        query,
        max_results=args.limit,
        search_fn=lambda q, limit: web_search(
            q,
            max_results=limit,
        ),
        reformulate_fn=lambda q: reformulate_queries_with_ollama(
            q,
            endpoint=endpoint,
            model=model,
            timeout_seconds=timeout,
            ssl_context=ssl_ctx,
        ),
        relevance_fn=lambda q, sources: filter_relevant_sources_with_ollama(
            q,
            sources,
            endpoint=endpoint,
            model=model,
            timeout_seconds=timeout,
            ssl_context=ssl_ctx,
        ),
        fetch_page_fn=lambda url: fetch_page_text(url, ssl_context=ssl_ctx),
        summarize_fn=lambda q, sources: summarize_web_sources_with_ollama(
            q,
            sources,
            endpoint=endpoint,
            model=model,
            timeout_seconds=max(1, settings.openclaw_local_text_timeout_seconds),
            ssl_context=ssl_ctx,
        ),
    )
    if args.json:
        print(
            json.dumps(
                {
                    "query": answer.query,
                    "summary": answer.summary,
                    "sources": [
                        {"title": source.title, "url": source.url, "snippet": source.snippet}
                        for source in answer.sources
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(format_web_research_answer(answer))
    return 0


def _maybe_start_reputation_agent(args: argparse.Namespace, settings: AssistantSettings) -> None:
    if not getattr(args, "with_reputation_agent", False):
        return
    err = check_prerequisites(settings.reputation_agent_admin_token or "")
    if err:
        logger.error("reputation_agent: cannot start — %s", err)
        print(f"[reputation-agent] WARNING: cannot start — {err}")
        return
    start_agent_thread(
        server_url=settings.reputation_agent_server_url,
        api_key=settings.reputation_agent_admin_token or "",
        poll_secs=settings.reputation_agent_poll_secs,
    )
    print(f"[reputation-agent] background thread started → {settings.reputation_agent_server_url}")
def _ensure_reputation_agent_started(args: argparse.Namespace, settings: AssistantSettings) -> None:
    if not getattr(args, "with_reputation_agent", False):
        return
    try:
        _, started_now = ensure_agent_thread(
            server_url=settings.reputation_agent_server_url,
            api_key=settings.reputation_agent_admin_token or "",
            poll_secs=settings.reputation_agent_poll_secs,
        )
    except RuntimeError as exc:
        logger.error("reputation_agent: cannot start — %s", exc)
        print(f"[reputation-agent] WARNING: cannot start — {exc}")
        return
    status = "started" if started_now else "already running"
    print(f"[reputation-agent] {status} -> {settings.reputation_agent_server_url}")


def _handle_serve_dashboard(
    args: argparse.Namespace,
    settings: AssistantSettings,
    registry: ToolRegistry,
) -> int:
    logger.info("CLI serve-dashboard command received host=%s port=%s open_browser=%s", args.host, args.port, args.open_browser)
    _ensure_reputation_agent_started(args, settings)
    return serve_dashboard(
        settings=settings,
        registry=registry,
        host=args.host,
        port=args.port,
        open_browser=args.open_browser,
    )


def _handle_telegram_poll(
    args: argparse.Namespace,
    settings: AssistantSettings,
    registry: ToolRegistry,
) -> int:
    logger.info(
        "CLI telegram-poll command received poll_timeout=%s notify_startup=%s keep_pending=%s with_reputation_agent=%s no_dashboard=%s",
        args.poll_timeout,
        args.notify_startup,
        args.keep_pending,
        getattr(args, "with_reputation_agent", False),
        getattr(args, "no_dashboard", False),
    )
    _ensure_reputation_agent_started(args, settings)
    if not getattr(args, "no_dashboard", False):
        _start_dashboard_background(
            args=args,
            settings=settings,
            registry=registry,
            port=getattr(args, "dashboard_port", 8765),
        )
    return run_telegram_polling(
        settings=settings,
        lookup_renderer=default_lookup_renderer(settings),
        board_loader=lambda: default_board_loader(settings),
        catalog_renderer=lambda: render_tool_catalog(registry),
        poll_timeout=args.poll_timeout,
        notify_startup=args.notify_startup,
        drop_pending_updates=not args.keep_pending,
    )


def _start_dashboard_background(
    *,
    args: argparse.Namespace,
    settings: AssistantSettings,
    registry: ToolRegistry,
    port: int = 8765,
) -> None:
    host = "127.0.0.1"
    url = f"http://{host}:{port}"

    def _run() -> None:
        try:
            serve_dashboard(
                settings=settings,
                registry=registry,
                host=host,
                port=port,
                open_browser=False,
            )
        except Exception:
            logger.exception("Background dashboard server failed")

    thread = threading.Thread(target=_run, name="dashboard-server", daemon=True)
    thread.start()
    # Give the server a moment to bind before opening the browser
    time.sleep(0.6)
    webbrowser.open(url)
    print(f"[dashboard] Server started at {url}")


def _handle_telegram_send_test(
    args: argparse.Namespace,
    settings: AssistantSettings,
) -> int:
    logger.info("CLI telegram-send-test command received custom_message=%s", bool(args.message))
    return send_telegram_test_message(settings=settings, message=args.message)


def _configure_reputation_agent_parser(
    parser: argparse.ArgumentParser,
    settings: AssistantSettings,
) -> None:
    parser.add_argument(
        "--server-url",
        default=settings.reputation_agent_server_url,
        help="reputation-snapshot server URL (default: REPUTATION_AGENT_SERVER_URL env or local http://127.0.0.1:5000)",
    )
    parser.add_argument(
        "--token",
        default=settings.reputation_agent_admin_token or "",
        help="Admin token (default: REPUTATION_AGENT_ADMIN_TOKEN env var)",
    )
    parser.add_argument(
        "--poll-secs",
        type=int,
        default=settings.reputation_agent_poll_secs,
        help="Polling interval in seconds (default: REPUTATION_AGENT_POLL_SECS env or 5)",
    )


def _configure_opportunity_agent_parser(
    parser: argparse.ArgumentParser,
    settings: AssistantSettings,
) -> None:
    parser.add_argument("--once", action="store_true", help="Run one opportunity pipeline tick and exit.")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=settings.opportunity_interval_seconds,
        help=f"Seconds between continuous opportunity scans (default: {settings.opportunity_interval_seconds})",
    )


def _configure_opportunity_status_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=10, help="Maximum candidates to show.")


def _handle_reputation_agent(
    args: argparse.Namespace,
    settings: AssistantSettings,
) -> int:
    token = args.token or settings.reputation_agent_admin_token or ""
    err = check_prerequisites(token)
    if err:
        print(f"ERROR: {err}")
        return 1
    logger.info(
        "CLI reputation-agent command received server_url=%s poll_secs=%s",
        args.server_url,
        args.poll_secs,
    )
    run_agent_loop(
        server_url=args.server_url,
        api_key=token,
        poll_secs=args.poll_secs,
    )
    return 0


def _handle_opportunity_agent(
    args: argparse.Namespace,
    settings: AssistantSettings,
) -> int:
    effective_settings = replace(settings, opportunity_interval_seconds=args.interval_seconds)
    logger.info(
        "CLI opportunity-agent command received once=%s interval_seconds=%s db=%s sns_db=%s",
        args.once,
        effective_settings.opportunity_interval_seconds,
        effective_settings.opportunity_db_path,
        effective_settings.sns_db_path,
    )
    stats = run_opportunity_agent(settings=effective_settings, once=args.once)
    if stats is not None:
        print(
            "opportunity-agent tick: "
            f"discovered={stats.discovered} "
            f"candidates_checked={stats.candidates_checked} "
            f"price_checks={stats.price_checks} "
            f"listings_checked={stats.listings_checked} "
            f"recommendations_sent={stats.recommendations_sent} "
            f"rejected={stats.rejected}"
        )
    return 0


def _handle_opportunity_status(
    args: argparse.Namespace,
    settings: AssistantSettings,
) -> int:
    print(format_opportunity_status(settings, limit=max(1, min(30, args.limit))))
    return 0


def _handle_sns_monitor_service(
    args: argparse.Namespace,
    settings: AssistantSettings,
) -> int:
    logger.info("CLI sns-monitor-service started")
    from .sns_monitor_service import run_sns_monitor_service
    return run_sns_monitor_service(settings)


def _handle_price_monitor_service(
    args: argparse.Namespace,
    settings: AssistantSettings,
) -> int:
    logger.info("CLI price-monitor-service started")
    from .price_monitor_service import run_price_monitor_service
    return run_price_monitor_service(settings)
