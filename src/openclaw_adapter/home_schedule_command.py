"""``/schedulehome`` command + callback handlers (issue #39).

Button-driven authoring of timed home schedules — no manual typing of times or
weekdays. The flow is:

1. ``/schedulehome`` lists schedules with per-row 立即執行 / 啟用·停用 / 刪除
   buttons plus 「➕ 新增排程」.
2. ➕ → a ➖HH➕ / ➖MM➕ / ✅下一步 time picker.
3. ✅下一步 → a weekday picker (個別星期多選 + 每天 / 平日 / 週末) and ✅完成.
4. ✅完成 creates the schedule and enters *capture mode*: the user then sends the
   slash commands to run (e.g. ``/music playbest``, ``/say 早安``) one per
   message, finishing with 「完成」. Capture is wired in telegram_bot.py's
   build_reply_plan so plain ``/`` messages append to the schedule.

All callback_data stays well under Telegram's 64-byte cap: the weekday set rides
as a 7-char on/off mask (index 0=Mon … 6=Sun), e.g. ``sh:r:07:30:1111100:ok``.
"""

from __future__ import annotations

import logging
from typing import Callable

from .home_schedule import (
    DAY_KEYS,
    DAY_LABELS,
    HomeScheduleStore,
    WEEKDAY_KEYS,
    WEEKEND_KEYS,
    days_label,
    run_schedule_commands,
)

logger = logging.getLogger(__name__)

_MINUTE_STEP = 5


def _short(text: str, limit: int = 16) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _mask_to_days(mask: str) -> list[str]:
    return [DAY_KEYS[i] for i, c in enumerate(mask) if i < len(DAY_KEYS) and c == "1"]


def _days_to_mask(days: list[str]) -> str:
    return "".join("1" if d in days else "0" for d in DAY_KEYS)


def _parse_time(value: str | None) -> tuple[int, int]:
    """Parse ``"HH:MM"`` to ``(hh, mm)``; fall back to 07:00 on anything odd."""
    try:
        hh_s, mm_s = (value or "").split(":", 1)
        hh, mm = int(hh_s), int(mm_s)
    except (ValueError, AttributeError):
        return 7, 0
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return 7, 0
    return hh, mm


def _clamp_mask(mask: str) -> str:
    """Coerce arbitrary callback input to a clean 7-char 0/1 mask."""
    cleaned = "".join("1" if c == "1" else "0" for c in (mask or ""))
    cleaned = (cleaned + "0000000")[:7]
    return cleaned


# --- rendering -------------------------------------------------------------
def render_list(store: HomeScheduleStore) -> tuple[str, dict]:
    entries = store.list()
    if not entries:
        text = "🏠 家庭排程\n目前沒有任何排程。按「➕ 新增排程」開始。"
    else:
        lines = ["🏠 家庭排程"]
        for e in entries:
            state = "🟢" if e.get("enabled", True) else "⚪"
            sched = e.get("schedule") or {}
            label = e.get("label") or e.get("id")
            n = len(e.get("commands") or [])
            lines.append(
                f"{state} {label} — {sched.get('time') or '--:--'} "
                f"{days_label(sched.get('days') or [])}（{n} 個指令）"
            )
        text = "\n".join(lines)

    rows: list[list[dict]] = []
    for e in entries:
        sid = str(e.get("id"))
        label = e.get("label") or sid
        enabled = e.get("enabled", True)
        toggle_text = "⏸ 停用" if enabled else "▶️ 啟用"
        toggle_cb = f"sh:{'off' if enabled else 'on'}:{sid}"
        rows.append([{"text": f"🚀 {_short(label)}", "callback_data": f"sh:run:{sid}"}])
        rows.append(
            [
                {"text": toggle_text, "callback_data": toggle_cb},
                {"text": "✏️", "callback_data": f"sh:edit:{sid}"},
                {"text": "🗑", "callback_data": f"sh:del:{sid}"},
            ]
        )
    rows.append([{"text": "➕ 新增排程", "callback_data": "sh:add"}])
    return text, {"inline_keyboard": rows}


def render_time_picker(hh: int, mm: int) -> tuple[str, dict]:
    base = f"sh:t:{hh:02d}:{mm:02d}"
    text = (
        f"🕐 設定時間：{hh:02d}:{mm:02d}\n"
        "用 ➖➕ 調整時與分（分以 5 分鐘為單位），按「下一步」選擇重複日。"
    )
    markup = {
        "inline_keyboard": [
            [
                {"text": "➖ 時", "callback_data": f"{base}:h-"},
                {"text": f"{hh:02d} 時", "callback_data": f"{base}:nop"},
                {"text": "➕ 時", "callback_data": f"{base}:h+"},
            ],
            [
                {"text": "➖ 分", "callback_data": f"{base}:m-"},
                {"text": f"{mm:02d} 分", "callback_data": f"{base}:nop"},
                {"text": "➕ 分", "callback_data": f"{base}:m+"},
            ],
            [{"text": "✅ 下一步", "callback_data": f"{base}:ok"}],
            [{"text": "✖️ 取消", "callback_data": "sh:cancel"}],
        ]
    }
    return text, markup


def render_recurrence_picker(hh: int, mm: int, mask: str) -> tuple[str, dict]:
    mask = _clamp_mask(mask)
    base = f"sh:r:{hh:02d}:{mm:02d}:{mask}"
    selected = _mask_to_days(mask)
    text = (
        f"📅 重複日：{days_label(selected)}（{hh:02d}:{mm:02d}）\n"
        "點星期可多選，或用 每天 / 平日 / 週末 快速設定，按「完成」建立排程。"
    )
    day_row = []
    for i, key in enumerate(DAY_KEYS):
        on = mask[i] == "1"
        label = ("✅" if on else "") + DAY_LABELS[key]
        day_row.append({"text": label, "callback_data": f"{base}:d{i}"})
    markup = {
        "inline_keyboard": [
            day_row,
            [
                {"text": "每天", "callback_data": f"{base}:da"},
                {"text": "平日", "callback_data": f"{base}:wk"},
                {"text": "週末", "callback_data": f"{base}:we"},
            ],
            [{"text": "✅ 完成", "callback_data": f"{base}:ok"}],
            [{"text": "✖️ 取消", "callback_data": "sh:cancel"}],
        ]
    }
    return text, markup


def render_edit_menu(entry: dict) -> tuple[str, dict]:
    sid = str(entry.get("id"))
    label = entry.get("label") or sid
    sched = entry.get("schedule") or {}
    text = (
        f"✏️ 編輯排程「{label}」\n"
        f"目前：{sched.get('time') or '--:--'} "
        f"{days_label(sched.get('days') or [])}\n"
        "要改什麼？"
    )
    markup = {
        "inline_keyboard": [
            [{"text": "🕐 改時間／星期", "callback_data": f"sh:edittime:{sid}"}],
            [{"text": "✏️ 改名稱", "callback_data": f"sh:rename:{sid}"}],
            [{"text": "↩️ 返回", "callback_data": "sh:list"}],
        ]
    }
    return text, markup


def _rename_hint(entry: dict) -> str:
    return (
        f"✏️ 重新命名排程「{entry.get('label') or entry.get('id')}」\n"
        "請直接傳新的名稱（一行文字）。輸入「取消」可放棄。"
    )


def _format_run(entry: dict, results: list[str]) -> str:
    label = entry.get("label") or entry.get("id")
    if not results:
        return f"已執行「{label}」（此排程沒有任何指令）。"
    body = "\n".join(f"• {r}" for r in results)
    return f"已執行「{label}」：\n{body}"


def _capture_hint(entry: dict) -> str:
    sched = entry.get("schedule") or {}
    return (
        f"✅ 已建立排程「{entry.get('label') or entry.get('id')}」"
        f"（{sched.get('time')} {days_label(sched.get('days') or [])}）。\n"
        "現在請直接傳要在該時間執行的指令，一則一則傳，例如：\n"
        "/music playbest\n/say 早安\n"
        "全部傳完後輸入「完成」即可。"
    )


# --- command handler -------------------------------------------------------
def build_schedulehome_handler(
    store: HomeScheduleStore, run_command: Callable[[str], str]
):
    """``/schedulehome [add|run <id>|on <id>|off <id>|delete <id>]``."""

    def handler(remainder: str, chat_id: str):  # noqa: ARG001
        parts = (remainder or "").strip().split()
        if not parts:
            return render_list(store)
        sub = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if sub == "add":
            return render_time_picker(7, 0)
        if sub == "add_for_wf":
            wf_id = arg
            if not wf_id:
                return "請指定 workflow id，例如 /schedulehome add_for_wf greeting_workflow"
            store.begin_pending_wf(chat_id, wf_id)
            return render_time_picker(7, 0)
        if sub in {"run", "on", "off", "delete", "del"} and not arg:
            return f"請指定排程 id，例如 /schedulehome {sub} sch_001"

        if sub == "run":
            entry = store.get(arg)
            if entry is None:
                return f"找不到排程：{arg}"
            results = run_schedule_commands(entry, run_command, str(chat_id))
            return _format_run(entry, results)
        if sub == "on":
            entry = store.set_enabled(arg, True)
            return f"已啟用排程：{arg}" if entry else f"找不到排程：{arg}"
        if sub == "off":
            entry = store.set_enabled(arg, False)
            return f"已停用排程：{arg}" if entry else f"找不到排程：{arg}"
        if sub in {"delete", "del"}:
            return f"已刪除排程：{arg}" if store.delete(arg) else f"找不到排程：{arg}"

        return (
            "用法：/schedulehome（列出）、/schedulehome add（新增）、"
            "/schedulehome run|on|off|delete <id>"
        )

    return handler


# --- callback handler ------------------------------------------------------
def build_schedulehome_callback_handler(
    store: HomeScheduleStore, run_command: Callable[[str], str]
):
    """``sh:`` prefix callback. Returns ``(toast, new_text, new_reply_markup)``."""

    def cb(payload: str, original_text: str, chat_id: str):  # noqa: ARG001
        parts = (payload or "").split(":")
        action = parts[0] if parts else ""

        if action == "add":
            text, markup = render_time_picker(7, 0)
            return None, text, markup

        if action == "cancel":
            store.end_capture(chat_id)
            store.end_edit(chat_id)
            store.end_pending_wf(chat_id)
            store.end_rename(chat_id)
            text, markup = render_list(store)
            return "已取消", text, markup

        if action == "done":
            sid = store.end_capture(chat_id)
            entry = store.get(sid) if sid else None
            n = len(entry.get("commands") or []) if entry else 0
            return f"✅ 排程設定完成，已加入 {n} 個指令。", None, None

        if action == "list":
            text, markup = render_list(store)
            return None, text, markup

        if action == "edit":
            sid = parts[1] if len(parts) > 1 else ""
            return _handle_edit_menu(sid)

        if action == "edittime":
            sid = parts[1] if len(parts) > 1 else ""
            return _handle_edit_start(sid, chat_id)

        if action == "rename":
            sid = parts[1] if len(parts) > 1 else ""
            return _handle_rename_start(sid, chat_id)

        if action == "t":
            return _handle_time(parts, chat_id)

        if action == "r":
            return _handle_recurrence(parts, chat_id)

        if action in {"on", "off", "del", "run"}:
            sid = parts[1] if len(parts) > 1 else ""
            return _handle_manage(action, sid, chat_id)

        return "未知的排程動作。", None, None

    def _handle_edit_menu(sid: str):
        # ✏️ on a row opens a small menu: change time/days, or rename. Keeps the
        # row buttons uncluttered while giving a path to edit the label.
        if not sid:
            return "缺少排程 id。", None, None
        entry = store.get(sid)
        if entry is None:
            return "找不到這個排程。", None, None
        text, markup = render_edit_menu(entry)
        return None, text, markup

    def _handle_rename_start(sid: str, chat_id: str):
        # 「✏️ 改名稱」: enter rename capture so the next plain-text message becomes
        # the new label (handled in telegram_bot.py build_reply_plan).
        if not sid:
            return "缺少排程 id。", None, None
        entry = store.get(sid)
        if entry is None:
            return "找不到這個排程。", None, None
        store.begin_rename(chat_id, sid)
        return "請輸入新名稱", _rename_hint(entry), None

    def _handle_edit_start(sid: str, chat_id: str):
        # 「🕐 改時間／星期」: re-run the time → recurrence pickers against an
        # existing schedule. We only re-set time/days here; the command list is
        # left as-is (editing commands is deliberately out of scope). The edit
        # target is held in store session state so picker "ok" updates instead
        # of creates.
        if not sid:
            return "缺少排程 id。", None, None
        entry = store.get(sid)
        if entry is None:
            return "找不到這個排程。", None, None
        store.begin_edit(chat_id, sid)
        hh, mm = _parse_time((entry.get("schedule") or {}).get("time"))
        text, markup = render_time_picker(hh, mm)
        return "編輯排程：請重設時間", text, markup

    def _handle_time(parts: list[str], chat_id: str):
        # parts = ["t", HH, MM, act]
        try:
            hh, mm = int(parts[1]), int(parts[2])
        except (IndexError, ValueError):
            return "時間格式錯誤。", None, None
        act = parts[3] if len(parts) > 3 else "nop"
        if act == "h+":
            hh = (hh + 1) % 24
        elif act == "h-":
            hh = (hh - 1) % 24
        elif act == "m+":
            mm = (mm + _MINUTE_STEP) % 60
        elif act == "m-":
            mm = (mm - _MINUTE_STEP) % 60
        elif act == "ok":
            # When editing, seed the recurrence picker with the schedule's
            # current days so the user only tweaks what changed.
            mask = "0000000"
            sid = store.edit_target(chat_id)
            if sid:
                entry = store.get(sid)
                if entry:
                    mask = _days_to_mask((entry.get("schedule") or {}).get("days") or [])
            text, markup = render_recurrence_picker(hh, mm, mask)
            return None, text, markup
        # nop or adjust → re-render time picker
        text, markup = render_time_picker(hh, mm)
        return None, text, markup

    def _handle_recurrence(parts: list[str], chat_id: str):
        # parts = ["r", HH, MM, mask, act]
        try:
            hh, mm = int(parts[1]), int(parts[2])
        except (IndexError, ValueError):
            return "時間格式錯誤。", None, None
        mask = _clamp_mask(parts[3] if len(parts) > 3 else "0000000")
        act = parts[4] if len(parts) > 4 else ""
        if act.startswith("d") and act[1:].isdigit():
            i = int(act[1:])
            if 0 <= i < 7:
                flipped = "1" if mask[i] == "0" else "0"
                mask = mask[:i] + flipped + mask[i + 1 :]
        elif act == "da":
            mask = "1111111"
        elif act == "wk":
            mask = _days_to_mask(WEEKDAY_KEYS)
        elif act == "we":
            mask = _days_to_mask(WEEKEND_KEYS)
        elif act == "ok":
            days = _mask_to_days(mask)
            if not days:
                text, markup = render_recurrence_picker(hh, mm, mask)
                return "請至少選擇一天", text, markup
            # Editing an existing schedule: update time/days in place, keep its
            # commands, and go straight back to the list (no command capture).
            sid = store.edit_target(chat_id)
            if sid:
                store.set_schedule(sid, time=f"{hh:02d}:{mm:02d}", days=days)
                store.end_edit(chat_id)
                text, markup = render_list(store)
                return "已更新排程", text, markup
            # Auto-fill path (web#9): workflow_id was pre-specified, skip capture.
            pending_wf = store.pending_wf_target(chat_id)
            if pending_wf:
                entry = store.add(
                    time=f"{hh:02d}:{mm:02d}",
                    days=days,
                    commands=[f"/workflow run {pending_wf}"],
                    label=pending_wf,
                )
                store.end_pending_wf(chat_id)
                list_text, markup = render_list(store)
                msg = (
                    f"✅ 已建立排程（{entry['id']}），將執行 /workflow run {pending_wf}\n\n"
                    + list_text
                )
                return None, msg, markup
            entry = store.add(time=f"{hh:02d}:{mm:02d}", days=days)
            store.begin_capture(chat_id, entry["id"])
            return "已建立排程", _capture_hint(entry), None
        text, markup = render_recurrence_picker(hh, mm, mask)
        return None, text, markup

    def _handle_manage(action: str, sid: str, chat_id: str):
        if not sid:
            return "缺少排程 id。", None, None
        if action == "run":
            entry = store.get(sid)
            if entry is None:
                return "找不到這個排程。", None, None
            results = run_schedule_commands(entry, run_command, str(chat_id))
            list_text, markup = render_list(store)
            return "已執行", _format_run(entry, results), markup
        if action == "on":
            store.set_enabled(sid, True)
            toast = "已啟用"
        elif action == "off":
            store.set_enabled(sid, False)
            toast = "已停用"
        else:  # del
            store.delete(sid)
            toast = "已刪除"
        text, markup = render_list(store)
        return toast, text, markup

    return cb
