from __future__ import annotations

import logging
from pathlib import Path

from .settings import AssistantSettings

_CONFIGURED_LOG_PATH: str | None = None
_MASK_IDENTIFIERS_IN_LOGS = True


def configure_logging(settings: AssistantSettings) -> None:
    global _CONFIGURED_LOG_PATH, _MASK_IDENTIFIERS_IN_LOGS

    target_log_path = str(Path(settings.log_file_path))
    if _CONFIGURED_LOG_PATH == target_log_path:
        return

    log_path = Path(settings.log_file_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(_level_from_name(settings.log_level))
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    _CONFIGURED_LOG_PATH = target_log_path
    _MASK_IDENTIFIERS_IN_LOGS = settings.log_level.strip().upper() != "DEBUG"


def mask_identifier(value: str | int | None) -> str:
    if value is None:
        return "n/a"
    text = str(value)
    if not _MASK_IDENTIFIERS_IN_LOGS:
        return text
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}***{text[-2:]}"


def trim_for_log(value: str, *, limit: int = 240) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3]}..."


def _level_from_name(value: str) -> int:
    return getattr(logging, value.strip().upper(), logging.INFO)
