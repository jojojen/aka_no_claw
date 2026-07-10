"""Local timed home scheduler for the ``/schedulehome`` command (issue #39).

A single-user, local-only scheduler that runs existing OpenClaw slash commands
(``/music``, ``/generateaudio``, ``/bluetooth`` …) at predefined times. Schedules persist
to a gitignored runtime JSON file so they survive bot restarts, and each entry
stores the slash commands verbatim — execution simply re-dispatches them through
the *same* command registry the Telegram bot already uses, so there is no second
implementation of any home action.

Design notes
------------
* **Store is a path-keyed singleton** (:func:`get_home_schedule_store`) so the
  command handler, the callback handler and the scheduler thread all observe the
  same in-memory capture state and the same persisted file.
* **Capture state is in-memory only.** After a schedule is created the user is
  put into "capture mode": subsequent plain ``/`` messages are appended to that
  schedule's command list until they send 「完成」. This state is intentionally
  not persisted — it is a transient authoring step.
* **Recurrence is a pure function** (:func:`schedule_due`) for easy testing; the
  scheduler thread only adds minute-level dedup on top of it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Monday-first so it lines up with datetime.weekday() (Mon=0 … Sun=6).
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = {
    "mon": "一",
    "tue": "二",
    "wed": "三",
    "thu": "四",
    "fri": "五",
    "sat": "六",
    "sun": "日",
}
WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri"]
WEEKEND_KEYS = ["sat", "sun"]

def system_timezone() -> str:
    """The host's IANA timezone name (e.g. ``Asia/Taipei``).

    The scheduler always fires on local wall-clock time (``datetime.now()``), so
    this string is informational — it records *which* zone a schedule's time was
    set against. Derived from the system, never hardcoded: read the IANA key from
    /etc/localtime's symlink, falling back to the libc abbreviation."""
    try:
        link = os.readlink("/etc/localtime")
    except OSError:
        link = ""
    if "zoneinfo/" in link:
        return link.split("zoneinfo/", 1)[1]
    return (time.tzname[0] if time.tzname else "") or "UTC"


DEFAULT_TIMEZONE = system_timezone()

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_ID_RE = re.compile(r"^sch_(\d+)$")


def _weekday_key(now: datetime) -> str:
    return DAY_KEYS[now.weekday()]


def normalize_days(days: list[str] | tuple[str, ...] | None) -> list[str]:
    """Keep only valid day keys, de-duplicated and ordered Mon→Sun."""
    if not days:
        return []
    seen = set(days)
    return [d for d in DAY_KEYS if d in seen]


def is_valid_time(value: str) -> bool:
    return bool(_TIME_RE.match(value or ""))


def days_label(days: list[str]) -> str:
    """Human label: 每天 / 平日 / 週末 / 一二三 …"""
    norm = normalize_days(days)
    if not norm:
        return "（未設定）"
    if norm == DAY_KEYS:
        return "每天"
    if norm == WEEKDAY_KEYS:
        return "平日"
    if norm == WEEKEND_KEYS:
        return "週末"
    return "".join(DAY_LABELS[d] for d in norm)


def schedule_due(entry: dict, now: datetime) -> bool:
    """Pure recurrence check: is *entry* due to fire at *now*?

    True only when the entry is enabled, *now*'s weekday is in its day set, and
    *now*'s HH:MM equals the configured time. Minute-level dedup (so it fires
    once, not every tick within the minute) is the scheduler's job, not this.
    """
    if not entry.get("enabled", True):
        return False
    sched = entry.get("schedule") or {}
    days = normalize_days(sched.get("days"))
    if _weekday_key(now) not in days:
        return False
    return now.strftime("%H:%M") == (sched.get("time") or "")


def next_fire_times(entry: dict, now: datetime, count: int = 3) -> list[datetime]:
    """Return the next *count* datetimes at which *entry* would fire.

    Scans forward day by day (up to 15 days max). Returns times in local time
    with the same tzinfo as *now*. If *now*'s time already passed today's
    configured time, starts searching from tomorrow. Returns [] if time is
    invalid, disabled, or days is empty.
    """
    sched = entry.get("schedule") or {}
    time_str = sched.get("time") or ""
    if not is_valid_time(time_str):
        return []
    if not entry.get("enabled", True):
        return []
    days = normalize_days(sched.get("days"))
    if not days:
        return []

    hh, mm = int(time_str[:2]), int(time_str[3:5])
    fires: list[datetime] = []
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)

    max_scans = 15
    for _ in range(max_scans):
        if _weekday_key(candidate) in days:
            fires.append(candidate)
            if len(fires) >= count:
                return fires
        candidate += timedelta(days=1)

    return fires


def run_schedule_commands(
    entry: dict, run_command: Callable[[str, str], str], chat_id: str = ""
) -> list[str]:
    """Run an entry's commands sequentially, returning one result line each.

    ``run_command(command, chat_id)`` is dispatched with a *real* chat id so
    commands that deliver to Telegram (e.g. ``/generateaudio`` sends a generated audio file to
    that chat) reach the right destination. A failing command never aborts the
    rest — its error becomes a result line so the user sees exactly which step
    failed (Rule C4: no silent substitution)."""
    results: list[str] = []
    for cmd in entry.get("commands", []) or []:
        try:
            results.append(str(run_command(cmd, chat_id)))
        except Exception as exc:  # noqa: BLE001 - surface, never abort the batch
            logger.exception("home schedule: command failed cmd=%s", cmd)
            results.append(f"⚠️ {cmd} 執行失敗：{exc}")
    return results


class HomeScheduleStore:
    """JSON-backed list of home schedules + transient capture state.

    Single-process, user-paced edits → a read-modify-write per mutation under a
    lock is plenty. Capture state lives only in memory."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        # chat_id -> schedule id currently being built (command capture mode).
        self._capture: dict[str, str] = {}
        # chat_id -> schedule id whose time/recurrence is being re-edited via the
        # time/recurrence pickers (no command capture; commands are left as-is).
        self._editing: dict[str, str] = {}
        # chat_id -> workflow_id pre-filled for auto-create path (web#9 B).
        # Set by add_for_wf; consumed and cleared on recurrence ok to skip capture.
        self._pending_wf: dict[str, str] = {}
        # chat_id -> schedule id whose label is being re-typed (rename capture):
        # the next plain-text message becomes the new label.
        self._renaming: dict[str, str] = {}

    # --- persistence ------------------------------------------------------
    def _load(self) -> list[dict]:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return []
        entries = data.get("entries") if isinstance(data, dict) else None
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    def _save(self, entries: list[dict]) -> None:
        p = Path(self._path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(p)

    # --- queries ----------------------------------------------------------
    def list(self) -> list[dict]:
        with self._lock:
            return self._load()

    def get(self, sid: str) -> dict | None:
        with self._lock:
            for e in self._load():
                if e.get("id") == sid:
                    return e
        return None

    def _next_id(self, entries: list[dict]) -> str:
        max_n = 0
        for e in entries:
            m = _ID_RE.match(str(e.get("id") or ""))
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f"sch_{max_n + 1:03d}"

    # --- mutations --------------------------------------------------------
    def add(
        self,
        *,
        label: str = "",
        time: str = "",
        days: list[str] | None = None,
        commands: list[str] | None = None,
        timezone: str = DEFAULT_TIMEZONE,
        enabled: bool = True,
    ) -> dict:
        with self._lock:
            entries = self._load()
            entry = {
                "id": self._next_id(entries),
                "label": label or "",
                "enabled": enabled,
                "timezone": timezone,
                "schedule": {"time": time, "days": normalize_days(days)},
                "commands": list(commands or []),
            }
            entries.append(entry)
            self._save(entries)
            return entry

    def _mutate(self, sid: str, fn: Callable[[dict], None]) -> dict | None:
        with self._lock:
            entries = self._load()
            for e in entries:
                if e.get("id") == sid:
                    fn(e)
                    self._save(entries)
                    return e
        return None

    def set_schedule(self, sid: str, *, time: str, days: list[str]) -> dict | None:
        return self._mutate(
            sid, lambda e: e.__setitem__("schedule", {"time": time, "days": normalize_days(days)})
        )

    def set_label(self, sid: str, label: str) -> dict | None:
        return self._mutate(sid, lambda e: e.__setitem__("label", label or ""))

    def set_enabled(self, sid: str, enabled: bool) -> dict | None:
        return self._mutate(sid, lambda e: e.__setitem__("enabled", bool(enabled)))

    def add_command(self, sid: str, command: str) -> dict | None:
        def _append(e: dict) -> None:
            cmds = list(e.get("commands") or [])
            cmds.append(command)
            e["commands"] = cmds

        return self._mutate(sid, _append)

    def clear_commands(self, sid: str) -> dict | None:
        return self._mutate(sid, lambda e: e.__setitem__("commands", []))

    def delete(self, sid: str) -> bool:
        with self._lock:
            entries = self._load()
            kept = [e for e in entries if e.get("id") != sid]
            if len(kept) == len(entries):
                return False
            self._save(kept)
            # Drop any capture/edit session pointing at the deleted schedule.
            self._capture = {c: s for c, s in self._capture.items() if s != sid}
            self._editing = {c: s for c, s in self._editing.items() if s != sid}
            self._renaming = {c: s for c, s in self._renaming.items() if s != sid}
            return True

    # --- transient capture state -----------------------------------------
    def begin_capture(self, chat_id: str, sid: str) -> None:
        with self._lock:
            self._capture[str(chat_id)] = sid

    def capture_target(self, chat_id: str) -> str | None:
        with self._lock:
            return self._capture.get(str(chat_id))

    def end_capture(self, chat_id: str) -> str | None:
        with self._lock:
            return self._capture.pop(str(chat_id), None)

    # --- transient edit state --------------------------------------------
    def begin_edit(self, chat_id: str, sid: str) -> None:
        with self._lock:
            self._editing[str(chat_id)] = sid

    def edit_target(self, chat_id: str) -> str | None:
        with self._lock:
            return self._editing.get(str(chat_id))

    def end_edit(self, chat_id: str) -> str | None:
        with self._lock:
            return self._editing.pop(str(chat_id), None)

    # --- transient pending-workflow state (web#9 auto-fill path) ----------
    def begin_pending_wf(self, chat_id: str, wf_id: str) -> None:
        with self._lock:
            self._pending_wf[str(chat_id)] = wf_id

    def pending_wf_target(self, chat_id: str) -> str | None:
        with self._lock:
            return self._pending_wf.get(str(chat_id))

    def end_pending_wf(self, chat_id: str) -> str | None:
        with self._lock:
            return self._pending_wf.pop(str(chat_id), None)

    # --- transient rename state (label re-typing capture) ----------------
    def begin_rename(self, chat_id: str, sid: str) -> None:
        with self._lock:
            self._renaming[str(chat_id)] = sid

    def rename_target(self, chat_id: str) -> str | None:
        with self._lock:
            return self._renaming.get(str(chat_id))

    def end_rename(self, chat_id: str) -> str | None:
        with self._lock:
            return self._renaming.pop(str(chat_id), None)


_STORES: dict[str, HomeScheduleStore] = {}
_STORES_LOCK = threading.Lock()


def get_home_schedule_store(path: str) -> HomeScheduleStore:
    """Path-keyed singleton so handler, callback and scheduler share state."""
    with _STORES_LOCK:
        store = _STORES.get(path)
        if store is None:
            store = HomeScheduleStore(path)
            _STORES[path] = store
        return store


def make_run_slash_command(
    command_handlers: dict,
) -> Callable[[str, str], str]:
    """Build a runner that executes a slash command through the SAME registry the
    Telegram dispatcher uses, so scheduled actions reuse existing handlers.

    ``run(command, chat_id)`` dispatches with a real chat id, so commands that
    deliver to Telegram (e.g. ``/generateaudio``) reach that chat. Background-flagged
    commands run synchronously here (the scheduler/manual run is already off the
    poll loop), a tuple ``(text, markup)`` result is reduced to its text, and a
    ``None`` result (handlers that deliver out-of-band, like ``/generateaudio``) becomes a
    short done marker — schedules report a text summary, not buttons.
    """

    def run(command: str, chat_id: str = "") -> str:
        content = (command or "").strip()
        if not content.startswith("/"):
            return f"略過（不是斜線指令）：{content}"
        name, _, remainder = content.partition(" ")
        name = name.split("@", 1)[0].lower()
        spec = command_handlers.get(name)
        if spec is None:
            return f"找不到指令：{name}"
        try:
            result = spec.handler(remainder.strip(), str(chat_id))
        except Exception as exc:  # noqa: BLE001
            logger.exception("home schedule: slash command failed cmd=%s", name)
            return f"{name} 失敗：{exc}"
        text = result[0] if isinstance(result, tuple) else result
        text = "" if text is None else str(text)
        return text or f"{name} 完成"

    return run


def _seconds_until_next_minute(now: datetime | None = None) -> float:
    now = now or datetime.now()
    return 60.0 - now.second - now.microsecond / 1_000_000.0


class HomeScheduleScheduler:
    """Daemon thread that fires due schedules once per matching minute.

    ``tick(now)`` is the pure, testable unit: it runs every enabled schedule that
    is due at *now* and dedups so a schedule fires once per minute even though the
    loop may wake slightly early/late. The thread just calls ``tick`` shortly
    after each minute boundary.
    """

    def __init__(
        self,
        *,
        store: HomeScheduleStore,
        run_command: Callable[[str, str], str],
        chat_id: str = "",
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._run_command = run_command
        self._chat_id = str(chat_id)
        self._notify = notify
        self._fired: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="home-schedule-scheduler", daemon=True
        )
        self._thread.start()
        logger.info("HomeScheduleScheduler started (minute-resolution).")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Wake a hair after the minute boundary so HH:MM has ticked over.
            if self._stop.wait(_seconds_until_next_minute() + 0.5):
                return
            try:
                self.tick(datetime.now())
            except Exception:  # noqa: BLE001
                logger.exception("HomeScheduleScheduler tick failed")

    def tick(self, now: datetime) -> list[str]:
        """Run all schedules due at *now*; return the ids that fired."""
        fired_ids: list[str] = []
        minute_key = now.strftime("%Y-%m-%dT%H:%M")
        for entry in self._store.list():
            if not schedule_due(entry, now):
                continue
            sid = str(entry.get("id") or "")
            if self._fired.get(sid) == minute_key:
                continue
            self._fired[sid] = minute_key
            self._run_entry(entry)
            fired_ids.append(sid)
        return fired_ids

    def run_now(self, entry: dict) -> list[str]:
        """Manual run (``/schedulehome run <id>``), bypassing the time check."""
        return self._run_entry(entry)

    def _run_entry(self, entry: dict) -> list[str]:
        label = entry.get("label") or entry.get("id") or "排程"
        results = run_schedule_commands(entry, self._run_command, self._chat_id)
        if self._notify:
            header = f"⏰ 已執行家庭排程「{label}」"
            body = "\n".join(f"• {r}" for r in results) if results else "（沒有任何指令）"
            try:
                self._notify(f"{header}\n{body}")
            except Exception:  # noqa: BLE001
                logger.exception("HomeScheduleScheduler: notify failed")
        return results
