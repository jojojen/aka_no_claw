from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_PATH = Path(".env")


@dataclass(frozen=True, slots=True)
class AssistantSettings:
    monitor_db_path: str = "data/monitor.sqlite3"
    yuyutei_user_agent: str = "OpenClawPriceMonitor/0.1 (+https://local-dev)"
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
    openclaw_local_text_timeout_seconds: int = 45
    openclaw_ca_bundle_path: str | None = None
    openclaw_tls_insecure_skip_verify: bool = False
    reputation_agent_server_url: str = "http://127.0.0.1:5000"
    reputation_agent_admin_token: str | None = None
    reputation_agent_poll_secs: int = 5
    monitor_env: str = "development"
    log_level: str = "INFO"
    log_file_path: str = "logs/openclaw.log"
    log_raw_result_limit: int = 20
    sns_db_path: str = "data/sns.sqlite3"
    knowledge_db_path: str = "data/knowledge.sqlite3"
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
    opportunity_official_store_provider_enabled: bool = True
    # ── SNS rule domain backfill / auto-discovery (Provider E + backfill) ────
    opportunity_sns_domain_backfill_enabled: bool = True
    opportunity_sns_auto_discovery_enabled: bool = True
    opportunity_sns_auto_discovery_interval_hours: int = 6
    opportunity_sns_auto_discovery_max_new_per_run: int = 2
    opportunity_sns_auto_discovery_min_confidence: float = 0.7


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
        monitor_db_path=os.getenv("MONITOR_DB_PATH", "data/monitor.sqlite3"),
        yuyutei_user_agent=os.getenv("YUYUTEI_USER_AGENT", "OpenClawPriceMonitor/0.1 (+https://local-dev)"),
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
        openclaw_local_text_timeout_seconds=_as_int(
            os.getenv("OPENCLAW_LOCAL_TEXT_TIMEOUT_SECONDS"),
            default=45,
        ),
        openclaw_ca_bundle_path=_none_if_empty(os.getenv("OPENCLAW_CA_BUNDLE_PATH")),
        openclaw_tls_insecure_skip_verify=_as_bool(os.getenv("OPENCLAW_TLS_INSECURE_SKIP_VERIFY")),
        reputation_agent_server_url=os.getenv(
            "REPUTATION_AGENT_SERVER_URL", "http://127.0.0.1:5000"
        ),
        reputation_agent_admin_token=_none_if_empty(os.getenv("REPUTATION_AGENT_ADMIN_TOKEN")),
        reputation_agent_poll_secs=_as_int(os.getenv("REPUTATION_AGENT_POLL_SECS"), default=5),
        monitor_env=os.getenv("MONITOR_ENV", "development"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_file_path=os.getenv("LOG_FILE_PATH", "logs/openclaw.log"),
        log_raw_result_limit=_as_int(os.getenv("LOG_RAW_RESULT_LIMIT"), default=20),
        sns_db_path=os.getenv("SNS_DB_PATH", "data/sns.sqlite3"),
        knowledge_db_path=os.getenv("KNOWLEDGE_DB_PATH", "data/knowledge.sqlite3"),
        sns_classifier_enabled=_as_bool(
            os.getenv("OPENCLAW_SNS_CLASSIFIER_ENABLED", "true")
        ),
        sns_classifier_min_score=_as_int(
            os.getenv("OPENCLAW_SNS_CLASSIFIER_MIN_SCORE"), default=60
        ),
        opportunity_agent_enabled=_as_bool(os.getenv("OPENCLAW_OPPORTUNITY_AGENT_ENABLED")),
        opportunity_db_path=os.getenv("OPENCLAW_OPPORTUNITY_DB_PATH", "data/opportunities.sqlite3"),
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
        opportunity_official_store_provider_enabled=_as_bool(
            os.getenv("OPENCLAW_OPPORTUNITY_OFFICIAL_STORE_PROVIDER_ENABLED"),
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


def _as_float(value: str | None, *, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default
