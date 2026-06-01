from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from openclaw_adapter.backup_command import (
    DEFAULT_BACKUP_DIR,
    BackupScheduler,
    build_backup_handler,
    build_recover_handler,
    run_backup,
    run_recover,
)


def _make_db(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE memory (id INTEGER PRIMARY KEY, note TEXT)")
    conn.executemany(
        "INSERT INTO memory (note) VALUES (?)", [(f"row-{i}",) for i in range(rows)]
    )
    conn.commit()
    conn.close()


def _project(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "project" / "data"
    tools_dir = tmp_path / "project" / "generated_tools"
    _make_db(data_dir / "knowledge.sqlite3", 3)
    _make_db(data_dir / "monitor.sqlite3", 5)
    _make_db(data_dir / "sns.sqlite3", 2)
    # A redundant manual snapshot that must be skipped.
    _make_db(data_dir / "monitor.sqlite3.bak_20260101", 5)
    # A learned tool: only its request spec matters; code/venv must NOT be copied.
    (tools_dir / "tool_a").mkdir(parents=True)
    (tools_dir / "tool_a" / "tool.py").write_text("print('hi')")
    (tools_dir / ".venv" / "lib").mkdir(parents=True)
    (tools_dir / ".venv" / "lib" / "big.so").write_text("x" * 1000)
    (tools_dir / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "slug": "tool_a",
                    "request": "找出50以內的質數印出來",
                    "requires": [],
                    "created_at": "2026-05-31T00:40:23+00:00",
                },
                {
                    "slug": "tsla_inc",
                    "request": "用 yfinance 查 TSLA FY2025 損益表",
                    "requires": ["yfinance"],
                    "created_at": "2026-05-31T00:18:47+00:00",
                },
            ]
        ),
        encoding="utf-8",
    )
    return data_dir, tools_dir


def test_run_backup_copies_live_dbs_and_specs_tools(tmp_path: Path) -> None:
    data_dir, tools_dir = _project(tmp_path)
    dest = tmp_path / "backup"

    report = run_backup(data_dir=data_dir, generated_tools_dir=tools_dir, dest=dest)

    names = {item.name for item in report.databases}
    assert names == {"knowledge.sqlite3", "monitor.sqlite3", "sns.sqlite3"}
    assert all(item.status == "sqlite" for item in report.databases)
    assert not report.errors

    # Backed-up copies are real, queryable SQLite snapshots.
    backed = sqlite3.connect(str(dest / "data" / "monitor.sqlite3"))
    assert backed.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 5
    backed.close()

    # .bak snapshot was skipped.
    assert not (dest / "data" / "monitor.sqlite3.bak_20260101").exists()

    # Tools: a SPEC doc is written, NO code / venv is copied.
    assert report.tools_spec_count == 2
    assert not (dest / "generated_tools").exists()
    spec = (dest / "generated_tools_spec.md").read_text(encoding="utf-8")
    assert "/new 找出50以內的質數印出來" in spec
    assert "yfinance" in spec
    assert "print('hi')" not in spec  # no code leaked


def test_run_backup_is_idempotent(tmp_path: Path) -> None:
    data_dir, tools_dir = _project(tmp_path)
    dest = tmp_path / "backup"

    run_backup(data_dir=data_dir, generated_tools_dir=tools_dir, dest=dest)
    report = run_backup(data_dir=data_dir, generated_tools_dir=tools_dir, dest=dest)

    assert not report.errors
    manifest = json.loads((dest / "backup_manifest.json").read_text())
    assert manifest["tools_spec_count"] == 2
    assert len(manifest["databases"]) == 3


def test_handler_uses_default_dest_from_settings(tmp_path: Path, monkeypatch) -> None:
    data_dir, _ = _project(tmp_path)
    dest = tmp_path / "ssd_backup"

    class _Settings:
        monitor_db_path = str(data_dir / "monitor.sqlite3")
        openclaw_backup_dir = str(dest)

    handler = build_backup_handler(_Settings())
    reply = handler("")

    assert "備份完成" in reply
    assert (dest / "data" / "monitor.sqlite3").exists()


def test_handler_rejects_missing_parent(tmp_path: Path) -> None:
    data_dir, _ = _project(tmp_path)

    class _Settings:
        monitor_db_path = str(data_dir / "monitor.sqlite3")
        openclaw_backup_dir = "/nope/definitely/not/mounted/claw_data"

    handler = build_backup_handler(_Settings())
    reply = handler("")
    assert "上層資料夾不存在" in reply


def test_default_backup_dir_constant() -> None:
    assert DEFAULT_BACKUP_DIR == "/Volumes/JEN_SSD/claw_data"


def test_recover_restores_into_fresh_project(tmp_path: Path) -> None:
    # First make a backup from a populated project.
    data_dir, tools_dir = _project(tmp_path)
    backup_dir = tmp_path / "backup"
    run_backup(data_dir=data_dir, generated_tools_dir=tools_dir, dest=backup_dir)

    # Simulate a freshly cloned project: empty data/, project root present.
    fresh_root = tmp_path / "fresh"
    fresh_data = fresh_root / "data"
    fresh_data.mkdir(parents=True)

    report = run_recover(
        data_dir=fresh_data, project_root=fresh_root, source=backup_dir
    )

    assert not report.errors
    assert {i.name for i in report.databases} == {
        "knowledge.sqlite3",
        "monitor.sqlite3",
        "sns.sqlite3",
    }
    restored = sqlite3.connect(str(fresh_data / "monitor.sqlite3"))
    assert restored.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 5
    restored.close()
    assert (fresh_root / "generated_tools_spec.md").exists()


def test_recover_skips_existing_unless_force(tmp_path: Path) -> None:
    data_dir, tools_dir = _project(tmp_path)
    backup_dir = tmp_path / "backup"
    run_backup(data_dir=data_dir, generated_tools_dir=tools_dir, dest=backup_dir)

    target_root = tmp_path / "live"
    target_data = target_root / "data"
    _make_db(target_data / "monitor.sqlite3", 999)  # existing live data

    # Without force: existing DB is preserved.
    report = run_recover(
        data_dir=target_data, project_root=target_root, source=backup_dir
    )
    assert "monitor.sqlite3" in report.skipped_existing
    kept = sqlite3.connect(str(target_data / "monitor.sqlite3"))
    assert kept.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 999
    kept.close()

    # With force: existing DB is overwritten by the backup (5 rows).
    report = run_recover(
        data_dir=target_data, project_root=target_root, source=backup_dir, force=True
    )
    assert not report.skipped_existing
    overwritten = sqlite3.connect(str(target_data / "monitor.sqlite3"))
    assert overwritten.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 5
    overwritten.close()


def test_recover_handler_missing_source(tmp_path: Path) -> None:
    data_dir, _ = _project(tmp_path)

    class _Settings:
        monitor_db_path = str(data_dir / "monitor.sqlite3")
        openclaw_backup_dir = str(tmp_path / "no_such_backup")

    handler = build_recover_handler(_Settings())
    assert "備份來源不存在" in handler("")


# ── BackupScheduler (A6) ────────────────────────────────────────────────────


def test_backup_scheduler_skips_when_mount_missing(tmp_path: Path) -> None:
    """_run_once must not call run_backup when dest parent is not mounted."""
    dest = tmp_path / "no_such_volume" / "claw_data"
    scheduler = BackupScheduler(
        data_dir=tmp_path / "data",
        generated_tools_dir=None,
        dest=dest,
    )
    with patch("openclaw_adapter.backup_command.run_backup") as mock_backup:
        scheduler._run_once()
    mock_backup.assert_not_called()


def test_backup_scheduler_runs_backup_when_mounted(tmp_path: Path) -> None:
    """_run_once calls run_backup when dest parent exists."""
    dest = tmp_path / "claw_data"
    scheduler = BackupScheduler(
        data_dir=tmp_path / "data",
        generated_tools_dir=None,
        dest=dest,
    )
    mock_report = MagicMock()
    mock_report.errors = []
    mock_report.databases = []
    mock_report.total_db_bytes = 0
    with patch("openclaw_adapter.backup_command.run_backup", return_value=mock_report) as mock_backup:
        scheduler._run_once()
    mock_backup.assert_called_once_with(
        data_dir=tmp_path / "data",
        generated_tools_dir=None,
        dest=dest,
    )


def test_backup_scheduler_logs_warning_on_errors(tmp_path: Path) -> None:
    """_run_once logs a warning (not exception) when run_backup returns errors."""
    import logging
    dest = tmp_path / "claw_data"
    scheduler = BackupScheduler(
        data_dir=tmp_path / "data",
        generated_tools_dir=None,
        dest=dest,
    )
    mock_report = MagicMock()
    mock_report.errors = ["knowledge.sqlite3: disk full"]
    mock_report.databases = []
    mock_report.total_db_bytes = 0
    with patch("openclaw_adapter.backup_command.run_backup", return_value=mock_report):
        with patch("openclaw_adapter.backup_command.logger") as mock_logger:
            scheduler._run_once()
    mock_logger.warning.assert_called()
