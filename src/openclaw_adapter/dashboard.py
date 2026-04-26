from __future__ import annotations

import json
import logging
import sqlite3
import webbrowser
from hashlib import sha1
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from assistant_runtime import AssistantSettings, ToolRegistry, build_ssl_context
from assistant_runtime.logging_utils import trim_for_log
from market_monitor import load_reference_sources
from market_monitor.http import HttpClient
from market_monitor.storage import MercariWatch, MonitorDatabase
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
    include_hot_cards: bool = True,
) -> dict[str, object]:
    reference_sources = load_reference_sources(reference_config_path)
    example_watchlist = _load_json_file(example_watchlist_path, default=[])
    runtime_stats = _load_runtime_stats(Path(settings.monitor_db_path))
    hot_card_service = hot_card_service or TcgHotCardService(
        http_client=HttpClient(
            user_agent=settings.yuyutei_user_agent,
            ssl_context=build_ssl_context(settings),
        )
    )

    hot_cards_error: str | None = None
    hot_card_boards: tuple[HotCardBoard, ...] = ()
    if include_hot_cards:
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


def build_hot_cards_payload(
    *,
    hot_card_service: TcgHotCardService,
) -> dict[str, object]:
    hot_cards_error: str | None = None
    hot_card_boards: tuple[HotCardBoard, ...] = ()
    try:
        hot_card_boards = hot_card_service.load_boards()
    except Exception as exc:  # pragma: no cover - remote-source failures are environment-dependent.
        hot_cards_error = str(exc)
    return {
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
    shared_hot_card_service = TcgHotCardService(
        http_client=HttpClient(
            user_agent=settings.yuyutei_user_agent,
            ssl_context=build_ssl_context(settings),
        )
    )

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
                self._write_json(
                    build_dashboard_payload(
                        settings=settings,
                        registry=registry,
                        hot_card_service=shared_hot_card_service,
                        include_hot_cards=False,
                    )
                )
                return
            if parsed.path == "/api/hot-cards":
                self._write_json(
                    build_hot_cards_payload(
                        hot_card_service=shared_hot_card_service,
                    )
                )
                return
            if parsed.path == "/api/tcg/lookup":
                self._handle_lookup(parsed.query)
                return
            if parsed.path == "/api/mercari-watchlist":
                self._handle_watchlist_get()
                return

            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/mercari-watchlist":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body.decode("utf-8"))
                except Exception:
                    self._write_json({"error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
                    return
                self._handle_watchlist_post(data)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_DELETE(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/mercari-watchlist/"):
                watch_id = parsed.path.removeprefix("/api/mercari-watchlist/")
                self._handle_watchlist_delete(watch_id)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_PATCH(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/mercari-watchlist/"):
                watch_id = parsed.path.removeprefix("/api/mercari-watchlist/")
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body.decode("utf-8"))
                except Exception:
                    self._write_json({"error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
                    return
                self._handle_watchlist_patch(watch_id, data)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def log_message(self, format: str, *args: object) -> None:
            return

        def _get_watch_db(self) -> MonitorDatabase:
            db = MonitorDatabase(settings.monitor_db_path)
            db.bootstrap()
            return db

        def _handle_watchlist_get(self) -> None:
            db = self._get_watch_db()
            watches = db.list_mercari_watchlist()
            payload = []
            for w in watches:
                hits = db.list_mercari_hits(w.watch_id, limit=5)
                payload.append({
                    "watch_id": w.watch_id,
                    "query": w.query,
                    "price_threshold_jpy": w.price_threshold_jpy,
                    "enabled": w.enabled,
                    "chat_id": w.chat_id,
                    "last_checked_at": w.last_checked_at,
                    "created_at": w.created_at,
                    "updated_at": w.updated_at,
                    "recent_hits": [
                        {
                            "mercari_item_id": h.mercari_item_id,
                            "title": h.title,
                            "price_jpy": h.price_jpy,
                            "url": h.url,
                            "thumbnail_url": h.thumbnail_url,
                            "first_seen_at": h.first_seen_at,
                            "notified": h.notified,
                        }
                        for h in hits
                    ],
                })
            self._write_json(payload)

        def _handle_watchlist_post(self, data: dict[str, object]) -> None:
            query = str(data.get("query") or "").strip()
            threshold_raw = data.get("price_threshold_jpy")
            chat_id = str(data.get("chat_id") or "dashboard").strip()
            if not query:
                self._write_json({"error": "query is required"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                threshold = int(str(threshold_raw).replace(",", ""))
            except (TypeError, ValueError):
                self._write_json({"error": "price_threshold_jpy must be an integer"}, status=HTTPStatus.BAD_REQUEST)
                return
            if threshold <= 0:
                self._write_json({"error": "price_threshold_jpy must be > 0"}, status=HTTPStatus.BAD_REQUEST)
                return
            watch_id = sha1(f"{chat_id}|{query}".encode()).hexdigest()[:16]
            watch = MercariWatch(
                watch_id=watch_id,
                query=query,
                price_threshold_jpy=threshold,
                enabled=True,
                chat_id=chat_id,
                last_checked_at=None,
                created_at="",
                updated_at="",
            )
            db = self._get_watch_db()
            db.add_mercari_watch(watch)
            logger.info("Dashboard watchlist add watch_id=%s query=%s threshold=%d", watch_id, query, threshold)
            self._write_json({"watch_id": watch_id, "query": query, "price_threshold_jpy": threshold}, status=HTTPStatus.CREATED)

        def _handle_watchlist_delete(self, watch_id: str) -> None:
            db = self._get_watch_db()
            deleted = db.delete_mercari_watch(watch_id)
            if deleted:
                logger.info("Dashboard watchlist delete watch_id=%s", watch_id)
                self._write_json({"deleted": True, "watch_id": watch_id})
            else:
                self._write_json({"error": f"watch_id '{watch_id}' not found"}, status=HTTPStatus.NOT_FOUND)

        def _handle_watchlist_patch(self, watch_id: str, data: dict[str, object]) -> None:
            db = self._get_watch_db()
            watch = db.get_mercari_watch(watch_id)
            if watch is None:
                self._write_json({"error": f"watch_id '{watch_id}' not found"}, status=HTTPStatus.NOT_FOUND)
                return
            if "enabled" in data:
                db.toggle_mercari_watch(watch_id, enabled=bool(data["enabled"]))
            query = str(data["query"]).strip() if "query" in data else None
            threshold = None
            if "price_threshold_jpy" in data:
                try:
                    threshold = int(str(data["price_threshold_jpy"]).replace(",", ""))
                except (TypeError, ValueError):
                    self._write_json({"error": "price_threshold_jpy must be integer"}, status=HTTPStatus.BAD_REQUEST)
                    return
            if query or threshold is not None:
                db.update_mercari_watch(watch_id, query=query, price_threshold_jpy=threshold)
            logger.info("Dashboard watchlist patch watch_id=%s", watch_id)
            self._write_json({"updated": True, "watch_id": watch_id})

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
            "available_item_count": len(board.items),
            "default_display_limit": _default_display_limit(len(board.items)),
            "allowed_display_limits": _allowed_display_limits(len(board.items)),
            "items": [
                {
                    "rank": item.rank,
                    "game": item.game,
                    "title": item.title,
                    "price_jpy": item.price_jpy,
                    "thumbnail_url": item.thumbnail_url,
                    "card_number": item.card_number,
                    "rarity": item.rarity,
                    "set_code": item.set_code,
                    "listing_count": item.listing_count,
                    "best_ask_jpy": item.best_ask_jpy,
                    "best_bid_jpy": item.best_bid_jpy,
                    "previous_bid_jpy": item.previous_bid_jpy,
                    "bid_ask_ratio": item.bid_ask_ratio,
                    "buy_support_score": item.buy_support_score,
                    "momentum_boost_score": item.momentum_boost_score,
                    "buy_signal_label": item.buy_signal_label,
                    "liquidity_score": item.hot_score,
                    "hot_score": item.hot_score,
                    "attention_score": item.attention_score,
                    "social_post_count": item.social_post_count,
                    "social_engagement_count": item.social_engagement_count,
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


def _allowed_display_limits(item_count: int) -> list[int]:
    if item_count <= 0:
        return []
    options = [count for count in (3, 5, 10, 20) if count <= item_count]
    if item_count not in options:
        options.append(item_count)
    return sorted(set(options))


def _default_display_limit(item_count: int) -> int:
    if item_count <= 0:
        return 0
    return 10 if item_count >= 10 else item_count
