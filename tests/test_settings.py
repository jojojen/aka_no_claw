from __future__ import annotations

import logging
from pathlib import Path

from assistant_runtime.logging_utils import configure_logging, mask_identifier
from assistant_runtime.settings import (
    AssistantSettings,
    get_settings,
    load_dotenv,
)


def test_load_dotenv_reads_monitor_settings(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "MONITOR_DB_PATH=data/custom.sqlite3",
                "YUYUTEI_USER_AGENT=CustomAgent/1.0",
                "OPENCLAW_TELEGRAM_BOT_TOKEN=secret-token",
                "OPENCLAW_TELEGRAM_SECURITY_UNAUTHORIZED_THRESHOLD=7",
                "OPENCLAW_TELEGRAM_SECURITY_HEALTH_CHECK_SECONDS=120",
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

    expected_root = Path(__file__).resolve().parents[1]
    assert settings.monitor_db_path == str((expected_root / "data/custom.sqlite3").resolve())
    assert settings.yuyutei_user_agent == "CustomAgent/1.0"
    assert settings.openclaw_telegram_bot_token == "secret-token"
    assert settings.openclaw_telegram_security_unauthorized_threshold == 7
    assert settings.openclaw_telegram_security_health_check_seconds == 120
    assert settings.openclaw_tls_insecure_skip_verify is True
    assert settings.log_file_path == str((expected_root / "logs/test-openclaw.log").resolve())
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


def test_mask_identifier_stays_masked_when_log_level_is_not_debug(tmp_path) -> None:
    log_path = tmp_path / "logs" / "openclaw-info.log"
    settings = AssistantSettings(log_file_path=str(log_path), log_level="INFO")

    configure_logging(settings)

    assert mask_identifier("-5123480") == "-5***80"


def test_mask_identifier_is_unmasked_when_log_level_is_debug(tmp_path) -> None:
    log_path = tmp_path / "logs" / "openclaw-debug.log"
    settings = AssistantSettings(log_file_path=str(log_path), log_level="DEBUG")

    configure_logging(settings)

    assert mask_identifier("-5123480") == "-5123480"


def test_get_settings_reads_local_vision_environment_keys(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_LOCAL_VISION_BACKEND", "ollama")
    monkeypatch.setenv("OPENCLAW_LOCAL_VISION_ENDPOINT", "http://127.0.0.1:11434")
    monkeypatch.setenv("OPENCLAW_LOCAL_VISION_MODEL", "qwen2.5vl:3b")
    monkeypatch.setenv("OPENCLAW_LOCAL_VISION_TIMEOUT_SECONDS", "120")

    settings = get_settings()

    assert settings.openclaw_local_vision_backend == "ollama"
    assert settings.openclaw_local_vision_endpoint == "http://127.0.0.1:11434"
    assert settings.openclaw_local_vision_model == "qwen2.5vl:3b"
    assert settings.openclaw_local_vision_timeout_seconds == 120


def test_get_settings_reads_local_text_router_environment_keys(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_LOCAL_TEXT_BACKEND", "ollama")
    monkeypatch.setenv("OPENCLAW_LOCAL_TEXT_ENDPOINT", "http://127.0.0.1:11434")
    monkeypatch.setenv("OPENCLAW_LOCAL_TEXT_MODEL", "gemma3:4b")
    monkeypatch.setenv("OPENCLAW_LOCAL_TEXT_TIMEOUT_SECONDS", "30")

    settings = get_settings()

    assert settings.openclaw_local_text_backend == "ollama"
    assert settings.openclaw_local_text_endpoint == "http://127.0.0.1:11434"
    assert settings.openclaw_local_text_model == "gemma3:4b"
    assert settings.openclaw_local_text_timeout_seconds == 30


def test_get_settings_reads_local_stt_environment_keys(monkeypatch, tmp_path) -> None:
    model_dir = tmp_path / "whisper"
    monkeypatch.setenv("OPENCLAW_STT_MODEL", "small")
    monkeypatch.setenv("OPENCLAW_STT_DEVICE", "cpu")
    monkeypatch.setenv("OPENCLAW_STT_COMPUTE_TYPE", "int8")
    monkeypatch.setenv("OPENCLAW_STT_DOWNLOAD_ROOT", str(model_dir))
    monkeypatch.setenv("OPENCLAW_STT_MAX_AUDIO_BYTES", "123456")
    monkeypatch.setenv("OPENCLAW_STT_MAX_DURATION_SECONDS", "45")

    settings = get_settings()

    assert settings.openclaw_stt_model == "small"
    assert settings.openclaw_stt_device == "cpu"
    assert settings.openclaw_stt_compute_type == "int8"
    assert settings.openclaw_stt_download_root == str(model_dir)
    assert settings.openclaw_stt_max_audio_bytes == 123456
    assert settings.openclaw_stt_max_duration_seconds == 45


def test_get_settings_reads_opencode_codegen_environment_keys(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_CODEGEN_BACKEND", "opencode")
    monkeypatch.setenv("OPENCLAW_OPENCODE_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("OPENCLAW_OPENCODE_MODEL", "big-pickle")
    monkeypatch.setenv("OPENCLAW_OPENCODE_API_KEY", "opencode-key")
    monkeypatch.setenv("OPENCLAW_OPENCODE_TIMEOUT_SECONDS", "321")

    settings = get_settings()

    assert settings.openclaw_codegen_backend == "opencode"
    assert settings.openclaw_opencode_base_url == "https://example.test/v1"
    assert settings.openclaw_opencode_model == "big-pickle"
    assert settings.openclaw_opencode_api_key == "opencode-key"
    assert settings.openclaw_opencode_timeout_seconds == 321


def test_get_settings_reads_gemini_environment_keys(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.setenv("OPENCLAW_GEMINI_PRIMARY_MODEL", "gemini-primary-test")
    monkeypatch.setenv("OPENCLAW_GEMINI_FLASH_MODEL", "gemini-flash-test")

    settings = get_settings()

    assert settings.openclaw_gemini_api_key == "google-key"
    assert settings.openclaw_gemini_primary_model == "gemini-primary-test"
    assert settings.openclaw_gemini_flash_model == "gemini-flash-test"


def test_get_settings_defaults_gemini_primary_to_flash(monkeypatch) -> None:
    monkeypatch.delenv("OPENCLAW_GEMINI_PRIMARY_MODEL", raising=False)
    monkeypatch.delenv("OPENCLAW_GEMINI_PRO_MODEL", raising=False)
    monkeypatch.delenv("OPENCLAW_GEMINI_FLASH_MODEL", raising=False)

    settings = get_settings()

    assert settings.openclaw_gemini_primary_model == "gemini-2.5-flash"
    assert settings.openclaw_gemini_flash_model == "gemini-2.5-flash"


def test_get_settings_accepts_legacy_gemini_pro_model_alias(monkeypatch) -> None:
    monkeypatch.delenv("OPENCLAW_GEMINI_PRIMARY_MODEL", raising=False)
    monkeypatch.setenv("OPENCLAW_GEMINI_PRO_MODEL", "gemini-pro-legacy")

    settings = get_settings()

    assert settings.openclaw_gemini_primary_model == "gemini-pro-legacy"


def test_get_settings_reads_local_tts_environment_keys(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_LOCAL_TTS_ENDPOINT", "http://127.0.0.1:10101")
    monkeypatch.setenv("OPENCLAW_LOCAL_TTS_TIMEOUT_SECONDS", "25")
    monkeypatch.setenv("OPENCLAW_LOCAL_TTS_SPEAKER_ID", "888753760")

    settings = get_settings()

    assert settings.openclaw_local_tts_endpoint == "http://127.0.0.1:10101"
    assert settings.openclaw_local_tts_timeout_seconds == 25
    assert settings.openclaw_local_tts_speaker_id == 888753760


def test_get_settings_resolves_runtime_db_paths_against_repo_root(monkeypatch) -> None:
    monkeypatch.setenv("SNS_DB_PATH", "data/sns.sqlite3")
    monkeypatch.setenv("SNS_INBOX_DB_PATH", "data/sns_inbox.sqlite3")
    monkeypatch.setenv("KNOWLEDGE_INBOX_DB_PATH", "data/knowledge_inbox.sqlite3")
    monkeypatch.setenv("OPENCLAW_OPPORTUNITY_DB_PATH", "data/opportunities.sqlite3")

    settings = get_settings()
    expected_root = Path(__file__).resolve().parents[1]

    assert settings.sns_db_path == str((expected_root / "data/sns.sqlite3").resolve())
    assert settings.sns_inbox_db_path == str((expected_root / "data/sns_inbox.sqlite3").resolve())
    assert settings.knowledge_inbox_db_path == str((expected_root / "data/knowledge_inbox.sqlite3").resolve())
    assert settings.opportunity_db_path == str((expected_root / "data/opportunities.sqlite3").resolve())


def test_get_settings_reads_web_event_journal_environment_keys(monkeypatch, tmp_path) -> None:
    event_dir = tmp_path / "web-events"
    monkeypatch.setenv("OPENCLAW_WEB_EVENT_DIR", str(event_dir))
    monkeypatch.setenv("OPENCLAW_WEB_EVENT_MAX_BYTES", "123456")
    monkeypatch.setenv("OPENCLAW_WEB_EVENT_MAX_AGE_DAYS", "14")
    monkeypatch.setenv("OPENCLAW_WEB_EVENT_MAX_PAYLOAD_BYTES", "4096")
    monkeypatch.setenv("OPENCLAW_WEB_CONTEXT_WINDOW_TOKENS", "8192")
    monkeypatch.setenv("OPENCLAW_WEB_CONTEXT_RESERVE_TOKENS", "1024")
    monkeypatch.setenv("OPENCLAW_WEB_CONTEXT_COMPACT_COOLDOWN_SECONDS", "33")

    settings = get_settings()

    assert settings.openclaw_web_event_dir == str(event_dir)
    assert settings.openclaw_web_event_max_bytes == 123456
    assert settings.openclaw_web_event_max_age_days == 14
    assert settings.openclaw_web_event_max_payload_bytes == 4096
    assert settings.openclaw_web_context_window_tokens == 8192
    assert settings.openclaw_web_context_reserve_tokens == 1024
    assert settings.openclaw_web_context_compact_cooldown_seconds == 33
