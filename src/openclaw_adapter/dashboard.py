from __future__ import annotations

import json
import logging
import sqlite3
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from assistant_runtime import AssistantSettings, ToolRegistry
from assistant_runtime.logging_utils import trim_for_log
from market_monitor import load_reference_sources
from tcg_tracker.hot_cards import HotCardBoard, TcgHotCardService

from .commands import lookup_card
from .formatters import lookup_result_payload, reference_sources_payload

ASSET_DIR = Path(__file__).with_name("dashboard_assets")
TEXT_ASSET_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}
logger = logging.getLogger(__name__)


def build_dashboard_payload(
    *,
    settings: AssistantSettings,
    registry: ToolRegistry,
    reference_config_path: str | Path = "config/reference_sources.json",
    example_watchlist_path: str | Path = "config/example_watchlist.json",
    hot_card_service: TcgHotCardService | None = None,
) -> dict[str, object]:
    reference_sources = load_reference_sources(reference_config_path)
    example_watchlist = _load_json_file(example_watchlist_path, default=[])
    runtime_stats = _load_runtime_stats(Path(settings.monitor_db_path))
    hot_card_service = hot_card_service or TcgHotCardService()

    hot_cards_error: str | None = None
    hot_card_boards: tuple[HotCardBoard, ...] = ()
    try:
        hot_card_boards = hot_card_service.load_boards()
    except Exception as exc:  # pragma: no cover - remote-source failures are environment-dependent.
        hot_cards_error = str(exc)

    return {
        "assistant": {
            "name": "OpenClaw Personal Assistant",
            "environment": settings.monitor_env,
            "log_level": settings.log_level,
            "monitor_db_path": settings.monitor_db_path,
            "telegram_configured": bool(
                settings.openclaw_telegram_chat_id and settings.openclaw_telegram_bot_token
            ),
        },
        "stats": runtime_stats["counts"],
        "tracked_items": runtime_stats["tracked_items"],
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "aliases": list(tool.aliases),
            }
            for tool in registry.tools()
        ],
        "reference_sources": reference_sources_payload(reference_sources),
        "example_watchlist": example_watchlist,
        "hot_cards": _hot_card_boards_payload(hot_card_boards),
        "hot_cards_error": hot_cards_error,
    }


def serve_dashboard(
    *,
    settings: AssistantSettings,
    registry: ToolRegistry,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> int:
    server = ThreadingHTTPServer((host, port), _build_handler(settings=settings, registry=registry))
    url = f"http://{host}:{port}"
    logger.info("Dashboard server starting host=%s port=%s open_browser=%s", host, port, open_browser)
    print(f"OpenClaw dashboard running at {url}")
    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _build_handler(*, settings: AssistantSettings, registry: ToolRegistry) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            logger.debug("Dashboard HTTP GET path=%s query=%s", parsed.path, trim_for_log(parsed.query, limit=240))

            if parsed.path == "/":
                self._serve_asset("index.html")
                return
            if parsed.path.startswith("/assets/"):
                self._serve_asset(parsed.path.removeprefix("/assets/"))
                return
            if parsed.path == "/api/dashboard":
                self._write_json(build_dashboard_payload(settings=settings, registry=registry))
                return
            if parsed.path == "/api/tcg/lookup":
                self._handle_lookup(parsed.query)
                return

            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def log_message(self, format: str, *args: object) -> None:
            return

        def _handle_lookup(self, query: str) -> None:
            params = parse_qs(query)
            game = _single_value(params, "game")
            name = _single_value(params, "name")
            logger.info(
                "Dashboard lookup received game=%s name=%s card_number=%s rarity=%s set_code=%s",
                game,
                name,
                _single_value(params, "card_number"),
                _single_value(params, "rarity"),
                _single_value(params, "set_code"),
            )
            if game not in {"pokemon", "ws"} or not name:
                self._write_json(
                    {
                        "error": "Both game and name are required. game must be one of pokemon or ws."
                    },
                    status=HTTPStatus.BAD_REQUEST,
                )
                return

            try:
                result = lookup_card(
                    db_path=settings.monitor_db_path,
                    game=game,
                    name=name,
                    card_number=_single_value(params, "card_number"),
                    rarity=_single_value(params, "rarity"),
                    set_code=_single_value(params, "set_code"),
                    set_name=_single_value(params, "set_name"),
                    persist=False,
                )
            except Exception as exc:  # pragma: no cover
                logger.exception("Dashboard lookup failed game=%s name=%s", game, name)
                self._write_json(
                    {"error": "Lookup failed.", "details": str(exc)},
                    status=HTTPStatus.BAD_GATEWAY,
                )
                return

            logger.info(
                "Dashboard lookup completed game=%s name=%s offers=%s fair_value=%s",
                game,
                name,
                len(result.offers),
                None if result.fair_value is None else result.fair_value.amount_jpy,
            )
            self._write_json(lookup_result_payload(result))

        def _serve_asset(self, asset_name: str) -> None:
            if "/" in asset_name or "\\" in asset_name:
                self.send_error(HTTPStatus.NOT_FOUND, "Asset not found")
                return

            asset_path = ASSET_DIR / asset_name
            if not asset_path.exists() or not asset_path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "Asset not found")
                return

            body = asset_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                TEXT_ASSET_TYPES.get(asset_path.suffix, "application/octet-stream"),
            )
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _write_json(self, payload: dict[str, object] | list[dict[str, object]], *, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return DashboardHandler


def _load_json_file(path: str | Path, *, default: Any) -> Any:
    file_path = Path(path)
    if not file_path.exists():
        return default
    return json.loads(file_path.read_text(encoding="utf-8"))


def _load_runtime_stats(db_path: Path) -> dict[str, object]:
    if not db_path.exists():
        return {
            "counts": {
                "tracked_items": 0,
                "watch_rules": 0,
                "source_offers": 0,
                "price_snapshots": 0,
            },
            "tracked_items": [],
        }

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        counts = {
            "tracked_items": _scalar_query(connection, "SELECT COUNT(*) FROM tracked_items"),
            "watch_rules": _scalar_query(connection, "SELECT COUNT(*) FROM watch_rules"),
            "source_offers": _scalar_query(connection, "SELECT COUNT(*) FROM source_offers"),
            "price_snapshots": _scalar_query(connection, "SELECT COUNT(*) FROM price_snapshots"),
        }
        tracked_items = [
            {
                "item_id": row["item_id"],
                "title": row["title"],
                "category": row["category"],
                "attributes": json.loads(row["attributes_json"]),
                "discount_threshold_pct": row["discount_threshold_pct"],
                "schedule_minutes": row["schedule_minutes"],
                "enabled": bool(row["enabled"]) if row["enabled"] is not None else None,
                "fair_value_jpy": row["fair_value_jpy"],
                "confidence": row["confidence"],
                "computed_at": row["computed_at"],
            }
            for row in connection.execute(
                """
                SELECT
                    tracked_items.item_id,
                    tracked_items.title,
                    tracked_items.category,
                    tracked_items.attributes_json,
                    watch_rules.discount_threshold_pct,
                    watch_rules.schedule_minutes,
                    watch_rules.enabled,
                    latest_snapshot.fair_value_jpy,
                    latest_snapshot.confidence,
                    latest_snapshot.computed_at
                FROM tracked_items
                LEFT JOIN watch_rules ON watch_rules.item_id = tracked_items.item_id
                LEFT JOIN (
                    SELECT ranked.item_id, ranked.fair_value_jpy, ranked.confidence, ranked.computed_at
                    FROM (
                        SELECT
                            item_id,
                            fair_value_jpy,
                            confidence,
                            computed_at,
                            ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY computed_at DESC) AS rank_index
                        FROM price_snapshots
                    ) AS ranked
                    WHERE ranked.rank_index = 1
                ) AS latest_snapshot ON latest_snapshot.item_id = tracked_items.item_id
                ORDER BY tracked_items.updated_at DESC
                LIMIT 8
                """
            )
        ]
        return {"counts": counts, "tracked_items": tracked_items}
    finally:
        connection.close()


def _scalar_query(connection: sqlite3.Connection, query: str) -> int:
    row = connection.execute(query).fetchone()
    return 0 if row is None else int(row[0])


def _single_value(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    if not values:
        return None
    value = values[0].strip()
    return value or None


def _hot_card_boards_payload(boards: tuple[HotCardBoard, ...]) -> list[dict[str, object]]:
    return [
        {
            "game": board.game,
            "label": board.label,
            "methodology": board.methodology,
            "generated_at": board.generated_at.isoformat(),
            "items": [
                {
                    "rank": item.rank,
                    "game": item.game,
                    "title": item.title,
                    "price_jpy": item.price_jpy,
                    "card_number": item.card_number,
                    "rarity": item.rarity,
                    "set_code": item.set_code,
                    "listing_count": item.listing_count,
                    "liquidity_score": item.hot_score,
                    "hot_score": item.hot_score,
                    "notes": list(item.notes),
                    "is_graded": item.is_graded,
                    "references": [
                        {
                            "label": reference.label,
                            "url": reference.url,
                        }
                        for reference in item.references
                    ],
                }
                for item in board.items
            ],
        }
        for board in boards
    ]
