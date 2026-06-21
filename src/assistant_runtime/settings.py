from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from market_monitor import browser_stealth as bs

DEFAULT_ENV_PATH = Path(".env")
_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class AssistantSettings:
    monitor_db_path: str = "data/monitor.sqlite3"
    yuyutei_user_agent: str = bs.MAC_CHROME_UA
    openclaw_telegram_chat_id: str | None = None  # primary (first) chat id
    openclaw_telegram_chat_ids: tuple[str, ...] = ()  # all allowed chat ids
    openclaw_telegram_bot_token: str | None = None
    openclaw_tesseract_path: str | None = None
    openclaw_tessdata_dir: str | None = None
    openclaw_local_vision_backend: str | None = None
    openclaw_local_vision_endpoint: str = "http://127.0.0.1:11434"
    openclaw_local_vision_model: str | None = None
    openclaw_local_vision_timeout_seconds: int = 180
    openclaw_local_text_backend: str | None = None
    openclaw_local_text_endpoint: str = "http://127.0.0.1:11434"
    openclaw_local_text_model: str | None = None
    openclaw_local_tts_endpoint: str = "http://127.0.0.1:10101"
    openclaw_local_tts_timeout_seconds: int = 20
    openclaw_local_tts_speaker_id: int | None = None
    # Fast, code-specialized tier-1 model for /new codegen. Escalates to the
    # (larger) openclaw_local_text_model only when this tier exhausts repairs.
    openclaw_codegen_fast_model: str | None = "qwen2.5-coder:7b"
    # /new codegen backend override. Empty/ollama preserves the local-only
    # behavior; opencode uses the OpenCode Zen OpenAI-compatible endpoint.
    openclaw_codegen_backend: str | None = None
    openclaw_opencode_base_url: str = "https://opencode.ai/zen/v1"
    openclaw_opencode_model: str = "big-pickle"
    openclaw_opencode_api_key: str | None = None
    openclaw_opencode_timeout_seconds: int = 900
    # /research appreciation enrichment backend, decoupled from /new's codegen
    # backend above. "opencode" routes the appreciation summariser to cloud
    # big-pickle (with single in-process local fallback); empty keeps it local.
    openclaw_research_cloud_enricher: str | None = None
    openclaw_local_text_timeout_seconds: int = 45
    # KB semantic retrieval. Multilingual embed model served by the local text
    # endpoint (Ollama). Empty string disables KB embedding (pure-lexical).
    openclaw_kb_embed_model: str = "bge-m3"
    # Minimum cosine score for the embedding intent fast-path to short-circuit a
    # zero-arg command (skipping the slow LLM router). Below this it falls
    # through to the LLM router. Lower = faster but riskier mis-routes.
    openclaw_intent_fastpath_min_score: float = 0.65
    # Minimum bge-m3 cosine similarity for a marketplace search title to count
    # as the queried product (research title_match). Below this the listing is
    # treated as a different item and dropped. Lower = higher recall / more
    # false positives.
    openclaw_research_title_match_threshold: float = 0.72
    openclaw_ca_bundle_path: str | None = None
    openclaw_tls_insecure_skip_verify: bool = False
    reputation_agent_server_url: str = "http://127.0.0.1:5000"
    reputation_agent_admin_token: str | None = None
    reputation_agent_poll_secs: int = 5
    reputation_agent_job_timeout_secs: float = 360.0
    monitor_env: str = "development"
    log_level: str = "INFO"
    log_file_path: str = "logs/openclaw.log"
    log_raw_result_limit: int = 20
    sns_db_path: str = "data/sns.sqlite3"
    sns_inbox_db_path: str = "data/sns_inbox.sqlite3"
    knowledge_db_path: str = "data/knowledge.sqlite3"
    market_entity_db_path: str = "data/market_entities.sqlite3"
    price_ledger_db_path: str = "data/price_ledger.sqlite3"
    sold_comp_db_path: str = "data/sold_comps.sqlite3"
    knowledge_inbox_db_path: str = "data/knowledge_inbox.sqlite3"
    opportunity_inbox_db_path: str = "data/opportunity_inbox.sqlite3"
    watch_inbox_db_path: str = "data/watch_inbox.sqlite3"
    quiz_db_path: str = "data/quiz.sqlite3"
    openclaw_backup_dir: str = "/Volumes/JEN_SSD/claw_data"
    openclaw_backup_hour: int = 23  # daily auto-backup fire time (local)
    sns_classifier_enabled: bool = True
    sns_classifier_min_score: int = 60
    opportunity_agent_enabled: bool = False
    opportunity_db_path: str = "data/opportunities.sqlite3"
    opportunity_interval_seconds: int = 900
    opportunity_llm_timeout_seconds: int = 180
    opportunity_sns_lookback_hours: int = 24
    opportunity_candidate_limit: int = 4
    opportunity_listing_limit: int = 5
    opportunity_candidate_check_interval_seconds: int = 1800
    opportunity_min_heat_score: float = 70.0
    opportunity_max_price_ratio: float = 0.85
    opportunity_min_price_confidence: float = 0.60
    opportunity_min_total_reviews: int = 30
    opportunity_min_positive_rate: float = 97.0
    # ── multi-source candidate providers ────────────────────────────────────
    opportunity_hot_card_provider_enabled: bool = True
    opportunity_hot_card_per_game_limit: int = 3
    opportunity_hot_card_min_score: float = 60.0
    opportunity_web_trend_provider_enabled: bool = True
    opportunity_web_trend_queries: tuple[str, ...] = ()
    opportunity_web_trend_results_per_query: int = 5
    # Throttle DuckDuckGo-backed discovery (trend sweep + per-candidate
    # enrichment) so the 15-min pipeline tick doesn't re-run searches every
    # tick; default 24h = one sweep/day. Between runs the providers replay
    # their cached candidates without issuing any HTTP search.
    opportunity_web_search_min_interval_seconds: int = 86400
    # Hard cap on the number of automated web searches, shared across all entry
    # points (trend sweep, candidate enrichment, SNS account discovery). Two
    # windows: at most `hourly_budget` searches per clock-hour AND at most
    # `daily_budget` per UTC day. The hourly cap is what prevents a burst (e.g.
    # the trend sweep's query list) from tripping an upstream IP rate-limit.
    opportunity_web_search_hourly_budget: int = 1
    opportunity_web_search_daily_budget: int = 10
    opportunity_official_store_provider_enabled: bool = True
    # ── collectible intelligence funnel (issue #8) ──────────────────────────
    # Standalone signal store shared (via the SQLite file) by the opportunity
    # daemon (writes official-store/marketplace signals), the telegram process
    # (writes SNS catalysts, reads for the daily digest). Lives next to the
    # opportunity DB.
    collectible_signal_db_path: str = "data/collectible_signals.sqlite3"
    collectible_signal_store_enabled: bool = True
    # ── SNS rule domain backfill / auto-discovery (Provider E + backfill) ────
    opportunity_sns_domain_backfill_enabled: bool = True
    opportunity_sns_auto_discovery_enabled: bool = True
    opportunity_sns_auto_discovery_interval_hours: int = 6
    opportunity_sns_auto_discovery_max_new_per_run: int = 2
    opportunity_sns_auto_discovery_min_confidence: float = 0.7
    # ── local music playback (issue #33) ────────────────────────────────────
    # Mac mini local music folder; /music plays .flac from here via afplay.
    openclaw_music_dir: str = "/Volumes/JEN_SSD/Music"
    # Index + player-state caches live under the gitignored .openclaw_tmp/ so no
    # cache/state is ever written into a git-tracked path.
    openclaw_music_index_path: str = ".openclaw_tmp/music_index.json"
    openclaw_music_player_state_path: str = ".openclaw_tmp/music_player_state.json"


def _resolve_runtime_path(value: str) -> str:
    """Resolve relative runtime paths against the repo root instead of cwd.

    Launchd and other background runners do not reliably start in the project
    directory, so keeping DB/log paths cwd-relative causes producer/consumer
    services to silently diverge onto different files or fail to create them.
    """
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((_REPO_ROOT / path).resolve())


def load_dotenv(path: str | Path = DEFAULT_ENV_PATH, *, override: bool = False) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_quotes(value.strip())
        if override or key not in os.environ:
            os.environ[key] = value


def get_settings() -> AssistantSettings:
    _raw_chat_ids = _getenv_any("OPENCLAW_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID")
    _parsed_chat_ids = _parse_chat_ids(_raw_chat_ids)
    return AssistantSettings(
        monitor_db_path=_resolve_runtime_path(os.getenv("MONITOR_DB_PATH", "data/monitor.sqlite3")),
        yuyutei_user_agent=os.getenv("YUYUTEI_USER_AGENT", bs.MAC_CHROME_UA),
        openclaw_telegram_chat_id=_parsed_chat_ids[0] if _parsed_chat_ids else None,
        openclaw_telegram_chat_ids=tuple(_parsed_chat_ids),
        openclaw_telegram_bot_token=_none_if_empty(
            _getenv_any("OPENCLAW_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
        ),
        openclaw_tesseract_path=_none_if_empty(
            _getenv_any("OPENCLAW_TESSERACT_PATH", "TESSERACT_PATH")
        ),
        openclaw_tessdata_dir=_none_if_empty(
            _getenv_any("OPENCLAW_TESSDATA_DIR", "TESSDATA_DIR")
        ),
        openclaw_local_vision_backend=_none_if_empty(os.getenv("OPENCLAW_LOCAL_VISION_BACKEND")),
        openclaw_local_vision_endpoint=os.getenv("OPENCLAW_LOCAL_VISION_ENDPOINT", "http://127.0.0.1:11434"),
        openclaw_local_vision_model=_none_if_empty(os.getenv("OPENCLAW_LOCAL_VISION_MODEL")),
        openclaw_local_vision_timeout_seconds=_as_int(
            os.getenv("OPENCLAW_LOCAL_VISION_TIMEOUT_SECONDS"),
            default=180,
        ),
        openclaw_local_text_backend=_none_if_empty(os.getenv("OPENCLAW_LOCAL_TEXT_BACKEND")),
        openclaw_local_text_endpoint=os.getenv("OPENCLAW_LOCAL_TEXT_ENDPOINT", "http://127.0.0.1:11434"),
        openclaw_local_text_model=_none_if_empty(os.getenv("OPENCLAW_LOCAL_TEXT_MODEL")),
        openclaw_local_tts_endpoint=os.getenv("OPENCLAW_LOCAL_TTS_ENDPOINT", "http://127.0.0.1:10101"),
        openclaw_local_tts_timeout_seconds=_as_int(
            os.getenv("OPENCLAW_LOCAL_TTS_TIMEOUT_SECONDS"),
            default=20,
        ),
        openclaw_local_tts_speaker_id=_as_optional_int(os.getenv("OPENCLAW_LOCAL_TTS_SPEAKER_ID")),
        openclaw_codegen_fast_model=_none_if_empty(
            os.getenv("OPENCLAW_CODEGEN_FAST_MODEL", "qwen2.5-coder:7b")
        ),
        openclaw_codegen_backend=_none_if_empty(os.getenv("OPENCLAW_CODEGEN_BACKEND")),
        openclaw_opencode_base_url=os.getenv(
            "OPENCLAW_OPENCODE_BASE_URL", "https://opencode.ai/zen/v1"
        ),
        openclaw_opencode_model=os.getenv("OPENCLAW_OPENCODE_MODEL", "big-pickle"),
        openclaw_opencode_api_key=_none_if_empty(os.getenv("OPENCLAW_OPENCODE_API_KEY")),
        openclaw_opencode_timeout_seconds=_as_int(
            os.getenv("OPENCLAW_OPENCODE_TIMEOUT_SECONDS"),
            default=900,
        ),
        openclaw_research_cloud_enricher=_none_if_empty(
            os.getenv("OPENCLAW_RESEARCH_CLOUD_ENRICHER")
        ),
        openclaw_local_text_timeout_seconds=_as_int(
            os.getenv("OPENCLAW_LOCAL_TEXT_TIMEOUT_SECONDS"),
            default=45,
        ),
        openclaw_kb_embed_model=os.getenv("OPENCLAW_KB_EMBED_MODEL", "bge-m3"),
        openclaw_intent_fastpath_min_score=_as_float(
            os.getenv("OPENCLAW_INTENT_FASTPATH_MIN_SCORE"),
            default=0.65,
        ),
        openclaw_research_title_match_threshold=_as_float(
            os.getenv("OPENCLAW_RESEARCH_TITLE_MATCH_THRESHOLD"),
            default=0.72,
        ),
        openclaw_ca_bundle_path=_none_if_empty(os.getenv("OPENCLAW_CA_BUNDLE_PATH")),
        openclaw_tls_insecure_skip_verify=_as_bool(os.getenv("OPENCLAW_TLS_INSECURE_SKIP_VERIFY")),
        reputation_agent_server_url=os.getenv(
            "REPUTATION_AGENT_SERVER_URL", "http://127.0.0.1:5000"
        ),
        reputation_agent_admin_token=_none_if_empty(os.getenv("REPUTATION_AGENT_ADMIN_TOKEN")),
        reputation_agent_poll_secs=_as_int(os.getenv("REPUTATION_AGENT_POLL_SECS"), default=5),
        reputation_agent_job_timeout_secs=_as_float(
            os.getenv("REPUTATION_AGENT_JOB_TIMEOUT_SECS"), default=240.0
        ),
        monitor_env=os.getenv("MONITOR_ENV", "development"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_file_path=_resolve_runtime_path(os.getenv("LOG_FILE_PATH", "logs/openclaw.log")),
        log_raw_result_limit=_as_int(os.getenv("LOG_RAW_RESULT_LIMIT"), default=20),
        sns_db_path=_resolve_runtime_path(os.getenv("SNS_DB_PATH", "data/sns.sqlite3")),
        sns_inbox_db_path=_resolve_runtime_path(os.getenv("SNS_INBOX_DB_PATH", "data/sns_inbox.sqlite3")),
        knowledge_db_path=_resolve_runtime_path(os.getenv("KNOWLEDGE_DB_PATH", "data/knowledge.sqlite3")),
        market_entity_db_path=_resolve_runtime_path(os.getenv("MARKET_ENTITY_DB_PATH", "data/market_entities.sqlite3")),
        price_ledger_db_path=_resolve_runtime_path(os.getenv("PRICE_LEDGER_DB_PATH", "data/price_ledger.sqlite3")),
        sold_comp_db_path=_resolve_runtime_path(os.getenv("SOLD_COMP_DB_PATH", "data/sold_comps.sqlite3")),
        knowledge_inbox_db_path=_resolve_runtime_path(os.getenv("KNOWLEDGE_INBOX_DB_PATH", "data/knowledge_inbox.sqlite3")),
        opportunity_inbox_db_path=_resolve_runtime_path(os.getenv("OPPORTUNITY_INBOX_DB_PATH", "data/opportunity_inbox.sqlite3")),
        watch_inbox_db_path=_resolve_runtime_path(os.getenv("WATCH_INBOX_DB_PATH", "data/watch_inbox.sqlite3")),
        quiz_db_path=_resolve_runtime_path(os.getenv("OPENCLAW_QUIZ_DB_PATH", "data/quiz.sqlite3")),
        openclaw_backup_dir=os.getenv("OPENCLAW_BACKUP_DIR", "/Volumes/JEN_SSD/claw_data"),
        openclaw_backup_hour=_as_int(
            os.getenv("OPENCLAW_BACKUP_HOUR"), default=23
        ),
        sns_classifier_enabled=_as_bool(
            os.getenv("OPENCLAW_SNS_CLASSIFIER_ENABLED", "true")
        ),
        sns_classifier_min_score=_as_int(
            os.getenv("OPENCLAW_SNS_CLASSIFIER_MIN_SCORE"), default=60
        ),
        opportunity_agent_enabled=_as_bool(os.getenv("OPENCLAW_OPPORTUNITY_AGENT_ENABLED")),
        opportunity_db_path=_resolve_runtime_path(os.getenv("OPENCLAW_OPPORTUNITY_DB_PATH", "data/opportunities.sqlite3")),
        opportunity_interval_seconds=_as_int(
            os.getenv("OPENCLAW_OPPORTUNITY_INTERVAL_SECONDS"),
            default=900,
        ),
        opportunity_llm_timeout_seconds=_as_int(
            os.getenv("OPENCLAW_OPPORTUNITY_LLM_TIMEOUT_SECONDS"),
            default=180,
        ),
        opportunity_sns_lookback_hours=_as_int(
            os.getenv("OPENCLAW_OPPORTUNITY_SNS_LOOKBACK_HOURS"),
            default=24,
        ),
        opportunity_candidate_limit=_as_int(
            os.getenv("OPENCLAW_OPPORTUNITY_CANDIDATE_LIMIT"),
            default=4,
        ),
        opportunity_listing_limit=_as_int(
            os.getenv("OPENCLAW_OPPORTUNITY_LISTING_LIMIT"),
            default=5,
        ),
        opportunity_candidate_check_interval_seconds=_as_int(
            os.getenv("OPENCLAW_OPPORTUNITY_CANDIDATE_CHECK_INTERVAL_SECONDS"),
            default=1800,
        ),
        opportunity_min_heat_score=_as_float(
            os.getenv("OPENCLAW_OPPORTUNITY_MIN_HEAT_SCORE"),
            default=70.0,
        ),
        opportunity_max_price_ratio=_as_float(
            os.getenv("OPENCLAW_OPPORTUNITY_MAX_PRICE_RATIO"),
            default=0.85,
        ),
        opportunity_min_price_confidence=_as_float(
            os.getenv("OPENCLAW_OPPORTUNITY_MIN_PRICE_CONFIDENCE"),
            default=0.60,
        ),
        opportunity_min_total_reviews=_as_int(
            os.getenv("OPENCLAW_OPPORTUNITY_MIN_TOTAL_REVIEWS"),
            default=30,
        ),
        opportunity_min_positive_rate=_as_float(
            os.getenv("OPENCLAW_OPPORTUNITY_MIN_POSITIVE_RATE"),
            default=97.0,
        ),
        opportunity_hot_card_provider_enabled=_as_bool(
            os.getenv("OPENCLAW_OPPORTUNITY_HOT_CARD_PROVIDER_ENABLED"),
            default=True,
        ),
        opportunity_hot_card_per_game_limit=_as_int(
            os.getenv("OPENCLAW_OPPORTUNITY_HOT_CARD_PER_GAME_LIMIT"),
            default=3,
        ),
        opportunity_hot_card_min_score=_as_float(
            os.getenv("OPENCLAW_OPPORTUNITY_HOT_CARD_MIN_SCORE"),
            default=60.0,
        ),
        opportunity_web_trend_provider_enabled=_as_bool(
            os.getenv("OPENCLAW_OPPORTUNITY_WEB_TREND_PROVIDER_ENABLED"),
            default=True,
        ),
        opportunity_web_trend_queries=_parse_csv(
            os.getenv("OPENCLAW_OPPORTUNITY_WEB_TREND_QUERIES"),
        ),
        opportunity_web_trend_results_per_query=_as_int(
            os.getenv("OPENCLAW_OPPORTUNITY_WEB_TREND_RESULTS_PER_QUERY"),
            default=5,
        ),
        opportunity_web_search_min_interval_seconds=_as_int(
            os.getenv("OPENCLAW_OPPORTUNITY_WEB_SEARCH_MIN_INTERVAL_SECONDS"),
            default=86400,
        ),
        opportunity_web_search_hourly_budget=_as_int(
            os.getenv("OPENCLAW_OPPORTUNITY_WEB_SEARCH_HOURLY_BUDGET"),
            default=1,
        ),
        opportunity_web_search_daily_budget=_as_int(
            os.getenv("OPENCLAW_OPPORTUNITY_WEB_SEARCH_DAILY_BUDGET"),
            default=10,
        ),
        opportunity_official_store_provider_enabled=_as_bool(
            os.getenv("OPENCLAW_OPPORTUNITY_OFFICIAL_STORE_PROVIDER_ENABLED"),
            default=True,
        ),
        collectible_signal_db_path=_resolve_runtime_path(
            os.getenv("OPENCLAW_COLLECTIBLE_SIGNAL_DB_PATH", "data/collectible_signals.sqlite3")
        ),
        collectible_signal_store_enabled=_as_bool(
            os.getenv("OPENCLAW_COLLECTIBLE_SIGNAL_STORE_ENABLED"),
            default=True,
        ),
        opportunity_sns_domain_backfill_enabled=_as_bool(
            os.getenv("OPENCLAW_OPPORTUNITY_SNS_DOMAIN_BACKFILL_ENABLED"),
            default=True,
        ),
        opportunity_sns_auto_discovery_enabled=_as_bool(
            os.getenv("OPENCLAW_OPPORTUNITY_SNS_AUTO_DISCOVERY_ENABLED"),
            default=True,
        ),
        opportunity_sns_auto_discovery_interval_hours=_as_int(
            os.getenv("OPENCLAW_OPPORTUNITY_SNS_AUTO_DISCOVERY_INTERVAL_HOURS"),
            default=6,
        ),
        opportunity_sns_auto_discovery_max_new_per_run=_as_int(
            os.getenv("OPENCLAW_OPPORTUNITY_SNS_AUTO_DISCOVERY_MAX_NEW_PER_RUN"),
            default=2,
        ),
        opportunity_sns_auto_discovery_min_confidence=_as_float(
            os.getenv("OPENCLAW_OPPORTUNITY_SNS_AUTO_DISCOVERY_MIN_CONFIDENCE"),
            default=0.7,
        ),
        openclaw_music_dir=os.getenv("OPENCLAW_MUSIC_DIR", "/Volumes/JEN_SSD/Music"),
        openclaw_music_index_path=_resolve_runtime_path(
            os.getenv("OPENCLAW_MUSIC_INDEX_PATH", ".openclaw_tmp/music_index.json")
        ),
        openclaw_music_player_state_path=_resolve_runtime_path(
            os.getenv("OPENCLAW_MUSIC_PLAYER_STATE_PATH", ".openclaw_tmp/music_player_state.json")
        ),
    )


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _none_if_empty(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return value


def _parse_chat_ids(value: str | None) -> list[str]:
    """Split a comma-separated chat ID string into a list of non-empty IDs."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _getenv_any(*keys: str) -> str | None:
    for key in keys:
        value = os.getenv(key)
        if value not in {None, ""}:
            return value
    return None


def _as_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    stripped = value.strip().lower()
    if stripped in {"1", "true", "yes", "on"}:
        return True
    if stripped in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _as_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def _as_optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _as_float(value: str | None, *, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default
