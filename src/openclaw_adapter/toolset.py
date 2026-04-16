from __future__ import annotations

import argparse
import logging
from pathlib import Path

from assistant_runtime import AssistantSettings, AssistantTool, ToolRegistry, get_settings

from .commands import list_reference_sources, lookup_card, seed_example_watchlist
from .dashboard import serve_dashboard
from .formatters import (
    format_lookup_result,
    format_reference_sources,
    lookup_result_to_json,
    reference_sources_to_json,
)
from .telegram_bot import (
    default_board_loader,
    default_lookup_renderer,
    run_telegram_polling,
    send_telegram_test_message,
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
    return registry


def render_tool_catalog(registry: ToolRegistry) -> str:
    lines = ["OpenClaw assistant tools:"]
    for tool in registry.tools():
        alias_text = f" | aliases: {', '.join(tool.aliases)}" if tool.aliases else ""
        lines.append(f"- {tool.name}: {tool.description}{alias_text}")
    return "\n".join(lines)


def _configure_lookup_card_parser(parser: argparse.ArgumentParser, settings: AssistantSettings) -> None:
    parser.add_argument("--db", default=settings.monitor_db_path)
    parser.add_argument("--game", choices=["pokemon", "ws"], required=True)
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
    parser.add_argument("--game", choices=["pokemon", "ws"])
    parser.add_argument("--kind")
    parser.add_argument("--role")
    parser.add_argument("--json", action="store_true")


def _configure_dashboard_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")


def _configure_telegram_poll_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--poll-timeout", type=int, default=20)
    parser.add_argument("--notify-startup", action="store_true")
    parser.add_argument("--keep-pending", action="store_true")


def _configure_telegram_send_test_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--message", default="OpenClaw Telegram test successful.")


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
        game=args.game,
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
        game=args.game,
        source_kind=args.kind,
        reference_role=args.role,
    )
    print(reference_sources_to_json(sources) if args.json else format_reference_sources(sources))
    return 0


def _handle_serve_dashboard(
    args: argparse.Namespace,
    settings: AssistantSettings,
    registry: ToolRegistry,
) -> int:
    logger.info("CLI serve-dashboard command received host=%s port=%s open_browser=%s", args.host, args.port, args.open_browser)
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
        "CLI telegram-poll command received poll_timeout=%s notify_startup=%s keep_pending=%s",
        args.poll_timeout,
        args.notify_startup,
        args.keep_pending,
    )
    return run_telegram_polling(
        settings=settings,
        lookup_renderer=default_lookup_renderer(settings),
        board_loader=default_board_loader,
        catalog_renderer=lambda: render_tool_catalog(registry),
        poll_timeout=args.poll_timeout,
        notify_startup=args.notify_startup,
        drop_pending_updates=not args.keep_pending,
    )


def _handle_telegram_send_test(
    args: argparse.Namespace,
    settings: AssistantSettings,
) -> int:
    logger.info("CLI telegram-send-test command received custom_message=%s", bool(args.message))
    return send_telegram_test_message(settings=settings, message=args.message)
