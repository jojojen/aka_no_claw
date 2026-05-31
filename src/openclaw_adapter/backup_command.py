"""`/backupclaw` — back up everything 龍蝦 learned that git does NOT track.

Goal: on a fresh machine, `git clone` + restore this backup must let the bot
seamlessly inherit all prior knowledge and market records. Git already carries
the code; this backup carries the *state* that lives outside it:

  - the RAG / knowledge store (knowledge.sqlite3)
  - market records + product (watch) tracking (monitor.sqlite3)
  - SNS tracked accounts + tweet/discovery preferences (sns.sqlite3)
  - opportunity targets + product preferences (opportunities.sqlite3)
  - collaboration outcomes / backfill + any other data/*.sqlite3
  - a SPEC of the tools learned via /new — the original natural-language
    requests, NOT the generated code (the code + per-tool venvs are pure
    regenerable bloat; re-feeding each request to /new rebuilds the tool).

Deliberately NOT backed up:
  - .env (secrets) — must be transferred separately/securely by the user.
  - generated tool *code* / venvs / caches — regenerable from the spec.

SQLite files are copied with the online backup API so a snapshot is consistent
even while the live bot keeps writing. The destination mirrors the source
layout (``<dest>/data/...``, ``<dest>/generated_tools/...``) so restoring is a
plain copy back into the project root.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BACKUP_DIR = "/Volumes/JEN_SSD/claw_data"

# SQLite sidecar / snapshot files we never copy verbatim.
_DB_SUFFIXES = (".sqlite3", ".db")


@dataclass(slots=True)
class BackupItem:
    name: str
    bytes: int
    status: str  # "sqlite", "copied", "restored", or "error: ..."


@dataclass(slots=True)
class BackupReport:
    dest: str
    started_at: str
    databases: list[BackupItem] = field(default_factory=list)
    tools_spec_count: int = 0
    tools_spec_path: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def total_db_bytes(self) -> int:
        return sum(item.bytes for item in self.databases if item.bytes > 0)


def _is_live_db(path: Path) -> bool:
    """True for a real DB file we should back up (not a .bak snapshot)."""
    # Skip dotfiles / macOS AppleDouble sidecars (e.g. ._monitor.sqlite3 that
    # the OS leaves on exFAT/NTFS external drives).
    if path.name.startswith("."):
        return False
    if path.suffix not in _DB_SUFFIXES:
        return False
    # data/monitor.sqlite3.bak_... has suffix ".bak_..." -> suffix not in set,
    # but be defensive about any ".bak" anywhere in the name.
    return ".bak" not in path.name


def _backup_sqlite(src: Path, dst: Path) -> int:
    """Consistent online-backup copy of a SQLite DB. Returns dest size."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    # read-only source so we never block or corrupt the live writer.
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return dst.stat().st_size


def _write_tools_spec(generated_tools_dir: Path, dest_path: Path) -> int:
    """Emit a regenerate-from-scratch SPEC of the /new-learned tools.

    Reads generated_tools/manifest.json (the source of truth: each tool's
    original natural-language request + pip requirements) and writes a
    human-readable Markdown spec. The bot reads this on a fresh machine and
    re-feeds each request to /new — the local model rebuilds equivalent tools.
    No generated code is stored. Returns the number of tools spec'd.
    """
    manifest_path = generated_tools_dir / "manifest.json"
    if not manifest_path.is_file():
        return 0
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(entries, list):
        return 0

    lines = [
        "# 龍蝦自學工具規格（/new generated tools）",
        "",
        "> 此檔只記錄每個工具的「原始需求規格」，**不含代碼**。",
        "> 換機後把每條 request 重新丟給 `/new`，本地模型即可重新生成等效工具。",
        "",
        f"> 共 {len(entries)} 個工具。產生時間："
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    count = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = str(entry.get("request") or entry.get("description") or "").strip()
        if not request:
            continue
        count += 1
        slug = entry.get("slug") or entry.get("id") or f"tool_{count}"
        requires = entry.get("requires") or []
        created = entry.get("created_at") or ""
        lines.append(f"## {count}. {slug}")
        lines.append("")
        lines.append("- 重建指令：`/new " + request.replace("\n", " ") + "`")
        if requires:
            lines.append(f"- 相依套件 (pip)：{', '.join(str(r) for r in requires)}")
        if created:
            lines.append(f"- 原始建立時間：{created}")
        lines.append("")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text("\n".join(lines), encoding="utf-8")
    return count


def run_backup(
    *,
    data_dir: Path,
    generated_tools_dir: Path | None,
    dest: Path,
) -> BackupReport:
    """Snapshot all non-git state into ``dest`` (idempotent / re-runnable)."""
    report = BackupReport(
        dest=str(dest),
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    dest.mkdir(parents=True, exist_ok=True)

    # 1) Databases (RAG, market, SNS, opportunities, collab, ...).
    dest_data = dest / "data"
    if data_dir.is_dir():
        for db_path in sorted(data_dir.iterdir()):
            if not db_path.is_file() or not _is_live_db(db_path):
                continue
            target = dest_data / db_path.name
            try:
                size = _backup_sqlite(db_path, target)
                report.databases.append(BackupItem(db_path.name, size, "sqlite"))
            except sqlite3.Error:
                # Not a valid/openable SQLite DB — fall back to a raw copy.
                try:
                    dest_data.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(db_path, target)
                    report.databases.append(
                        BackupItem(db_path.name, target.stat().st_size, "copied")
                    )
                except OSError as exc:
                    report.databases.append(BackupItem(db_path.name, -1, f"error: {exc}"))
                    report.errors.append(f"{db_path.name}: {exc}")
            except OSError as exc:
                report.databases.append(BackupItem(db_path.name, -1, f"error: {exc}"))
                report.errors.append(f"{db_path.name}: {exc}")
    else:
        report.errors.append(f"data dir not found: {data_dir}")

    # 2) SPEC of /new-learned tools (request only, no code — code is regenerable).
    if generated_tools_dir is not None and generated_tools_dir.is_dir():
        spec_path = dest / "generated_tools_spec.md"
        try:
            count = _write_tools_spec(generated_tools_dir, spec_path)
            report.tools_spec_count = count
            report.tools_spec_path = str(spec_path) if count else None
        except OSError as exc:
            report.errors.append(f"tools_spec: {exc}")

    # 3) Manifest for traceability / restore guidance.
    manifest = {
        "started_at": report.started_at,
        "dest": report.dest,
        "databases": [
            {"name": i.name, "bytes": i.bytes, "status": i.status}
            for i in report.databases
        ],
        "tools_spec_count": report.tools_spec_count,
        "tools_spec_path": report.tools_spec_path,
        "errors": report.errors,
        "note": (
            ".env and generated tool code are intentionally excluded; "
            "transfer .env separately and regenerate tools from the spec via /new."
        ),
    }
    try:
        (dest / "backup_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        report.errors.append(f"manifest: {exc}")

    return report


def _human_bytes(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def _format_report(report: BackupReport) -> str:
    lines = [f"✅ 龍蝦資料備份完成 → {report.dest}"]
    ok_dbs = [i for i in report.databases if not i.status.startswith("error")]
    lines.append(
        f"資料庫 {len(ok_dbs)} 個（{_human_bytes(report.total_db_bytes)}）："
    )
    for item in report.databases:
        mark = "•" if not item.status.startswith("error") else "⚠️"
        size = _human_bytes(item.bytes) if item.bytes >= 0 else "失敗"
        lines.append(f"  {mark} {item.name} ({size})")
    if report.tools_spec_count:
        lines.append(
            f"自學工具規格：{report.tools_spec_count} 個工具的需求已寫入 "
            "generated_tools_spec.md（只存規格、不存代碼）。"
        )
    lines.append("ℹ️ .env（密鑰）未備份，換機時請另外安全帶過去。")
    lines.append(
        "還原：把備份的 data/ 複製回專案根目錄；自學工具照 spec 用 /new 重新生成即可。"
    )
    if report.errors:
        lines.append("⚠️ 部分項目失敗：")
        lines.extend(f"  - {e}" for e in report.errors)
    return "\n".join(lines)


def build_backup_handler(settings) -> Callable[[str], str]:
    """Return a `/backupclaw` handler bound to the project's paths.

    The handler accepts an optional destination path as its argument; with no
    argument it uses ``settings.openclaw_backup_dir`` (default
    ``/Volumes/JEN_SSD/claw_data``).
    """
    data_dir = Path(settings.monitor_db_path).resolve().parent
    project_root = data_dir.parent
    generated_tools_dir = project_root / "generated_tools"
    default_dest = getattr(settings, "openclaw_backup_dir", DEFAULT_BACKUP_DIR) or DEFAULT_BACKUP_DIR

    def handler(remainder: str) -> str:
        dest = Path(remainder.strip()).expanduser() if remainder.strip() else Path(default_dest)
        parent = dest.parent
        if not parent.exists():
            return (
                f"❌ 備份目的地的上層資料夾不存在：{parent}\n"
                f"（預設外接碟 {default_dest} 沒掛上嗎？可指定 /backupclaw <路徑>）"
            )
        try:
            report = run_backup(
                data_dir=data_dir,
                generated_tools_dir=generated_tools_dir,
                dest=dest,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("backup failed dest=%s", dest)
            return f"❌ 備份失敗：{exc}"
        return _format_report(report)

    return handler


# ── recover ─────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class RecoverReport:
    source: str
    started_at: str
    databases: list[BackupItem] = field(default_factory=list)
    spec_restored: bool = False
    skipped_existing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def run_recover(
    *,
    data_dir: Path,
    project_root: Path,
    source: Path,
    force: bool = False,
) -> RecoverReport:
    """Restore a backup made by :func:`run_backup` into the project.

    Copies ``<source>/data/*.sqlite3`` back into ``data_dir`` and drops the
    learned-tools spec at the project root. Designed for a freshly re-cloned
    project. To avoid clobbering a live machine, an existing non-empty DB is
    skipped unless ``force`` is set.
    """
    report = RecoverReport(
        source=str(source),
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    src_data = source / "data"
    if not src_data.is_dir():
        report.errors.append(f"找不到備份資料：{src_data}")
        return report

    data_dir.mkdir(parents=True, exist_ok=True)
    for db_path in sorted(src_data.iterdir()):
        if not db_path.is_file() or not _is_live_db(db_path):
            continue
        target = data_dir / db_path.name
        if target.exists() and target.stat().st_size > 0 and not force:
            report.skipped_existing.append(db_path.name)
            continue
        try:
            shutil.copy2(db_path, target)
            report.databases.append(
                BackupItem(db_path.name, target.stat().st_size, "restored")
            )
        except OSError as exc:
            report.databases.append(BackupItem(db_path.name, -1, f"error: {exc}"))
            report.errors.append(f"{db_path.name}: {exc}")

    spec_src = source / "generated_tools_spec.md"
    if spec_src.is_file():
        try:
            shutil.copy2(spec_src, project_root / "generated_tools_spec.md")
            report.spec_restored = True
        except OSError as exc:
            report.errors.append(f"generated_tools_spec.md: {exc}")

    return report


def _format_recover_report(report: RecoverReport, *, default_source: str) -> str:
    if report.errors and not report.databases and not report.skipped_existing:
        head = "❌ 還原失敗"
        body = "\n".join(f"  - {e}" for e in report.errors)
        hint = f"（預設來源 {default_source} 沒掛上嗎？可指定 /clawrecover <路徑>）"
        return f"{head}\n{body}\n{hint}"

    lines = [f"✅ 龍蝦資料還原完成 ← {report.source}"]
    restored = [i for i in report.databases if i.status == "restored"]
    if restored:
        lines.append(f"已還原資料庫 {len(restored)} 個（{_human_bytes(sum(i.bytes for i in restored))}）：")
        for item in report.databases:
            mark = "•" if item.status == "restored" else "⚠️"
            size = _human_bytes(item.bytes) if item.bytes >= 0 else "失敗"
            lines.append(f"  {mark} {item.name} ({size})")
    if report.spec_restored:
        lines.append("已還原 generated_tools_spec.md，照裡面的 /new 指令即可重建自學工具。")
    if report.skipped_existing:
        lines.append(
            "⚠️ 下列資料庫已存在且非空，未覆蓋（避免蓋掉現有資料）："
        )
        lines.extend(f"  - {n}" for n in report.skipped_existing)
        lines.append("若確定要覆蓋，請執行：/clawrecover force（或 /clawrecover <路徑> force）")
    lines.append("ℹ️ .env（密鑰）不在備份內，請另外確認已就位。")
    lines.append("🔁 還原後請重啟 bot，讓它重新載入這些資料庫。")
    if report.errors:
        lines.append("⚠️ 部分項目失敗：")
        lines.extend(f"  - {e}" for e in report.errors)
    return "\n".join(lines)


def build_recover_handler(settings) -> Callable[[str], str]:
    """Return a `/clawrecover` handler bound to the project's paths.

    Argument forms (order-insensitive): an optional source path and an optional
    ``force`` keyword. No path → ``settings.openclaw_backup_dir``.
    """
    data_dir = Path(settings.monitor_db_path).resolve().parent
    project_root = data_dir.parent
    default_source = getattr(settings, "openclaw_backup_dir", DEFAULT_BACKUP_DIR) or DEFAULT_BACKUP_DIR

    def handler(remainder: str) -> str:
        force = False
        path_token = ""
        for token in remainder.split():
            if token.lower() == "force":
                force = True
            elif not path_token:
                path_token = token
        source = Path(path_token).expanduser() if path_token else Path(default_source)
        if not source.is_dir():
            return (
                f"❌ 備份來源不存在：{source}\n"
                f"（預設外接碟 {default_source} 沒掛上嗎？可指定 /clawrecover <路徑>）"
            )
        try:
            report = run_recover(
                data_dir=data_dir,
                project_root=project_root,
                source=source,
                force=force,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("recover failed source=%s", source)
            return f"❌ 還原失敗：{exc}"
        return _format_recover_report(report, default_source=default_source)

    return handler


def _main(argv: list[str]) -> int:
    """CLI: `python -m openclaw_adapter.backup_command {backup|recover} [path] [force]`.

    Useful on a freshly cloned machine — run `recover` from the shell BEFORE
    starting the bot, so the DBs exist when the bot opens them at startup.
    """
    from assistant_runtime import get_settings, load_dotenv

    if not argv or argv[0] not in {"backup", "recover"}:
        print("usage: python -m openclaw_adapter.backup_command {backup|recover} [path] [force]")
        return 2
    load_dotenv()
    settings = get_settings()
    action, rest = argv[0], " ".join(argv[1:])
    handler = build_backup_handler(settings) if action == "backup" else build_recover_handler(settings)
    print(handler(rest))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
