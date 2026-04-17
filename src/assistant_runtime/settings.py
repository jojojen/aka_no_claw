from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_PATH = Path(".env")


@dataclass(frozen=True, slots=True)
class AssistantSettings:
    monitor_db_path: str = "data/monitor.sqlite3"
    yuyutei_user_agent: str = "OpenClawPriceMonitor/0.1 (+https://local-dev)"
    openclaw_telegram_chat_id: str | None = None
    openclaw_telegram_bot_token: str | None = None
    openclaw_tesseract_path: str | None = None
    openclaw_tessdata_dir: str | None = None
    openclaw_local_vision_backend: str | None = None
    openclaw_local_vision_endpoint: str = "http://127.0.0.1:11434"
    openclaw_local_vision_model: str | None = None
    openclaw_local_vision_timeout_seconds: int = 120
    openclaw_ca_bundle_path: str | None = None
    openclaw_tls_insecure_skip_verify: bool = False
    monitor_env: str = "development"
    log_level: str = "INFO"
    log_file_path: str = "logs/openclaw.log"
    log_raw_result_limit: int = 20


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
    return AssistantSettings(
        monitor_db_path=os.getenv("MONITOR_DB_PATH", "data/monitor.sqlite3"),
        yuyutei_user_agent=os.getenv("YUYUTEI_USER_AGENT", "OpenClawPriceMonitor/0.1 (+https://local-dev)"),
        openclaw_telegram_chat_id=_none_if_empty(
            _getenv_any("OPENCLAW_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID")
        ),
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
            default=120,
        ),
        openclaw_ca_bundle_path=_none_if_empty(os.getenv("OPENCLAW_CA_BUNDLE_PATH")),
        openclaw_tls_insecure_skip_verify=_as_bool(os.getenv("OPENCLAW_TLS_INSECURE_SKIP_VERIFY")),
        monitor_env=os.getenv("MONITOR_ENV", "development"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_file_path=os.getenv("LOG_FILE_PATH", "logs/openclaw.log"),
        log_raw_result_limit=_as_int(os.getenv("LOG_RAW_RESULT_LIMIT"), default=20),
    )


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _none_if_empty(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return value


def _getenv_any(*keys: str) -> str | None:
    for key in keys:
        value = os.getenv(key)
        if value not in {None, ""}:
            return value
    return None


def _as_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default
