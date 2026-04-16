from __future__ import annotations

import logging
from pathlib import Path

from assistant_runtime.logging_utils import configure_logging
from assistant_runtime.settings import AssistantSettings, get_settings, load_dotenv


def test_load_dotenv_reads_monitor_settings(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "MONITOR_DB_PATH=data/custom.sqlite3",
                "YUYUTEI_USER_AGENT=CustomAgent/1.0",
                "OPENCLAW_TELEGRAM_BOT_TOKEN=secret-token",
                "OPENCLAW_TLS_INSECURE_SKIP_VERIFY=1",
                "LOG_FILE_PATH=logs/test-openclaw.log",
                "LOG_RAW_RESULT_LIMIT=7",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("MONITOR_DB_PATH", raising=False)
    monkeypatch.delenv("YUYUTEI_USER_AGENT", raising=False)
    monkeypatch.delenv("OPENCLAW_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_TLS_INSECURE_SKIP_VERIFY", raising=False)

    load_dotenv(env_file)
    settings = get_settings()

    assert settings.monitor_db_path == "data/custom.sqlite3"
    assert settings.yuyutei_user_agent == "CustomAgent/1.0"
    assert settings.openclaw_telegram_bot_token == "secret-token"
    assert settings.openclaw_tls_insecure_skip_verify is True
    assert settings.log_file_path == "logs/test-openclaw.log"
    assert settings.log_raw_result_limit == 7


def test_get_settings_accepts_telegram_alias_environment_keys(monkeypatch) -> None:
    monkeypatch.delenv("OPENCLAW_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("OPENCLAW_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "alias-token")

    settings = get_settings()

    assert settings.openclaw_telegram_chat_id == "123456"
    assert settings.openclaw_telegram_bot_token == "alias-token"


def test_configure_logging_creates_log_file(tmp_path) -> None:
    log_path = tmp_path / "logs" / "openclaw.log"
    settings = AssistantSettings(log_file_path=str(log_path), log_level="INFO")

    configure_logging(settings)
    logging.getLogger("test.settings").info("logging smoke test")

    assert log_path.exists()
    assert "logging smoke test" in log_path.read_text(encoding="utf-8")
