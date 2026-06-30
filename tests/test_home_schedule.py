"""Tests for the local /schedulehome scheduler (aka_no_claw #39).

Covers: JSON persistence + restart recovery, id generation, recurrence
evaluation (weekday/time/enabled), sequential command execution with fail-soft,
the slash-command runner that re-dispatches through the existing registry,
enable/disable, manual run, scheduler minute-dedup, capture state, and the
button-driven command/callback handlers (time picker → recurrence picker →
capture).
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from openclaw_adapter import home_schedule as hs
from openclaw_adapter import home_schedule_command as hc


@pytest.fixture
def store(tmp_path):
    return hs.HomeScheduleStore(str(tmp_path / "home_schedules.json"))


# --- persistence + ids -----------------------------------------------------
def test_add_persists_and_reloads(tmp_path):
    path = str(tmp_path / "s.json")
    store = hs.HomeScheduleStore(path)
    entry = store.add(label="起床", time="07:30", days=["mon", "tue"], commands=["/music playbest"])
    assert entry["id"] == "sch_001"
    # A fresh store over the same file (simulates a bot restart) sees it.
    reloaded = hs.HomeScheduleStore(path)
    got = reloaded.get("sch_001")
    assert got is not None
    assert got["label"] == "起床"
    assert got["schedule"] == {"time": "07:30", "days": ["mon", "tue"]}
    assert got["commands"] == ["/music playbest"]


def test_ids_increment(store):
    a = store.add(time="07:00", days=["mon"])
    b = store.add(time="08:00", days=["tue"])
    assert (a["id"], b["id"]) == ("sch_001", "sch_002")


def test_missing_file_loads_empty(tmp_path):
    store = hs.HomeScheduleStore(str(tmp_path / "nope.json"))
    assert store.list() == []


def test_corrupt_file_fails_soft(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    store = hs.HomeScheduleStore(str(p))
    assert store.list() == []


# --- recurrence ------------------------------------------------------------
def test_schedule_due_matches_weekday_and_time(store):
    # 2026-06-22 is a Monday.
    entry = store.add(time="07:30", days=["mon"])
    assert hs.schedule_due(entry, datetime(2026, 6, 22, 7, 30)) is True
    assert hs.schedule_due(entry, datetime(2026, 6, 22, 7, 31)) is False  # wrong minute
    assert hs.schedule_due(entry, datetime(2026, 6, 23, 7, 30)) is False  # Tuesday


def test_schedule_due_false_when_disabled(store):
    entry = store.add(time="07:30", days=["mon"], enabled=False)
    assert hs.schedule_due(entry, datetime(2026, 6, 22, 7, 30)) is False


def test_normalize_days_orders_and_dedups():
    assert hs.normalize_days(["sun", "mon", "mon", "bogus"]) == ["mon", "sun"]


def test_days_label_named_sets():
    assert hs.days_label(hs.DAY_KEYS) == "每天"
    assert hs.days_label(hs.WEEKDAY_KEYS) == "平日"
    assert hs.days_label(hs.WEEKEND_KEYS) == "週末"
    assert hs.days_label(["mon", "wed"]) == "一三"


# --- sequential execution + runner -----------------------------------------
def test_run_schedule_commands_runs_in_order(store):
    entry = store.add(time="07:00", days=["mon"], commands=["/a", "/b", "/c"])
    seen: list[str] = []

    def run(cmd: str, chat_id: str) -> str:
        seen.append(cmd)
        return f"ok:{cmd}"

    results = hs.run_schedule_commands(entry, run)
    assert seen == ["/a", "/b", "/c"]
    assert results == ["ok:/a", "ok:/b", "ok:/c"]


def test_run_schedule_commands_threads_chat_id(store):
    entry = store.add(time="07:00", days=["mon"], commands=["/say hi"])
    seen: list[str] = []

    def run(cmd: str, chat_id: str) -> str:
        seen.append(chat_id)
        return "ok"

    hs.run_schedule_commands(entry, run, "chat-42")
    assert seen == ["chat-42"]


def test_run_schedule_commands_failsoft_continues(store):
    entry = store.add(time="07:00", days=["mon"], commands=["/a", "/boom", "/c"])

    def run(cmd: str, chat_id: str) -> str:
        if cmd == "/boom":
            raise RuntimeError("kaboom")
        return f"ok:{cmd}"

    results = hs.run_schedule_commands(entry, run)
    assert results[0] == "ok:/a"
    assert "kaboom" in results[1]
    assert results[2] == "ok:/c"  # later command still runs


def _fake_handlers(seen_chat: list | None = None):
    def music(remainder, chat_id):
        if seen_chat is not None:
            seen_chat.append(chat_id)
        return f"music:{remainder}"

    def say(remainder, chat_id):
        # Mirrors the real /say: delivers audio out-of-band, returns None.
        return None

    return {
        "/music": SimpleNamespace(handler=music),
        "/say": SimpleNamespace(handler=say),
    }


def test_make_run_slash_command_dispatches_to_registry():
    run = hs.make_run_slash_command(_fake_handlers())
    assert run("/music playbest", "chat1") == "music:playbest"


def test_make_run_slash_command_passes_chat_id():
    seen: list[str] = []
    run = hs.make_run_slash_command(_fake_handlers(seen))
    run("/music x", "chat-9")
    assert seen == ["chat-9"]


def test_make_run_slash_command_none_result_becomes_done_marker():
    # /say returns None on success (audio sent out-of-band) → no "None" leak.
    run = hs.make_run_slash_command(_fake_handlers())
    assert run("/say 早安", "chat1") == "/say 完成"


def test_make_run_slash_command_unknown_and_non_slash():
    run = hs.make_run_slash_command(_fake_handlers())
    assert "找不到指令" in run("/nope", "chat1")
    assert "略過" in run("not a command", "chat1")


def test_make_run_slash_command_sees_later_additions():
    """Regression: /workflow is added to command_handlers AFTER make_run_slash_command
    is called (same pattern as telegram_bot._build_registries). The closed-over dict
    reference must pick up the late addition at dispatch time."""
    handlers: dict = {}
    run = hs.make_run_slash_command(handlers)  # called before /workflow exists

    # Simulate the late registration that happens in _build_registries
    calls: list[tuple] = []

    def workflow_handler(remainder, chat_id):
        calls.append((remainder, chat_id))
        return f"wf:{remainder}"

    handlers["/workflow"] = SimpleNamespace(handler=workflow_handler)

    result = run("/workflow run wf-morning", "chat-42")
    assert result == "wf:run wf-morning"
    assert calls == [("run wf-morning", "chat-42")]



# --- enable/disable + manual run -------------------------------------------
def test_set_enabled_persists(tmp_path):
    path = str(tmp_path / "s.json")
    store = hs.HomeScheduleStore(path)
    store.add(time="07:00", days=["mon"])
    store.set_enabled("sch_001", False)
    assert hs.HomeScheduleStore(path).get("sch_001")["enabled"] is False
    store.set_enabled("sch_001", True)
    assert hs.HomeScheduleStore(path).get("sch_001")["enabled"] is True


def test_delete_removes(store):
    store.add(time="07:00", days=["mon"])
    assert store.delete("sch_001") is True
    assert store.get("sch_001") is None
    assert store.delete("sch_001") is False


# --- scheduler -------------------------------------------------------------
def test_scheduler_tick_fires_due_once_per_minute(store):
    store.add(time="07:30", days=["mon"], commands=["/music x"])
    ran: list[str] = []
    sched = hs.HomeScheduleScheduler(store=store, run_command=lambda c, cid: ran.append(c) or "ok")
    now = datetime(2026, 6, 22, 7, 30)
    assert sched.tick(now) == ["sch_001"]
    # Second tick within the same minute must NOT re-fire.
    assert sched.tick(now) == []
    assert ran == ["/music x"]


def test_scheduler_tick_skips_disabled(store):
    store.add(time="07:30", days=["mon"], commands=["/music x"], enabled=False)
    sched = hs.HomeScheduleScheduler(store=store, run_command=lambda c, cid: "ok")
    assert sched.tick(datetime(2026, 6, 22, 7, 30)) == []


def test_scheduler_run_now_ignores_time(store):
    entry = store.add(time="07:30", days=["mon"], commands=["/a", "/b"])
    ran: list[str] = []
    sched = hs.HomeScheduleScheduler(store=store, run_command=lambda c, cid: ran.append(c) or "ok")
    results = sched.run_now(entry)
    assert ran == ["/a", "/b"]
    assert results == ["ok", "ok"]


def test_scheduler_notify_receives_summary(store):
    store.add(label="晨間", time="07:30", days=["mon"], commands=["/music x"])
    notes: list[str] = []
    sched = hs.HomeScheduleScheduler(
        store=store, run_command=lambda c, cid: "done", notify=notes.append
    )
    sched.tick(datetime(2026, 6, 22, 7, 30))
    assert len(notes) == 1
    assert "晨間" in notes[0]


# --- capture state ---------------------------------------------------------
def test_capture_lifecycle(store):
    store.add(time="07:00", days=["mon"])
    assert store.capture_target("chat1") is None
    store.begin_capture("chat1", "sch_001")
    assert store.capture_target("chat1") == "sch_001"
    assert store.end_capture("chat1") == "sch_001"
    assert store.capture_target("chat1") is None


def test_delete_clears_capture(store):
    store.add(time="07:00", days=["mon"])
    store.begin_capture("chat1", "sch_001")
    store.delete("sch_001")
    assert store.capture_target("chat1") is None


# --- singleton + system tz -------------------------------------------------
def test_get_home_schedule_store_is_singleton(tmp_path):
    path = str(tmp_path / "single.json")
    assert hs.get_home_schedule_store(path) is hs.get_home_schedule_store(path)


def test_system_timezone_nonempty():
    assert hs.system_timezone()


def test_default_timezone_from_system():
    assert hs.DEFAULT_TIMEZONE == hs.system_timezone()


# --- command + callback handlers -------------------------------------------
def test_handler_lists_empty(store):
    handler = hc.build_schedulehome_handler(store, lambda c, cid: "ok")
    text, markup = handler("", "chat1")
    assert "沒有任何排程" in text
    assert markup["inline_keyboard"][-1][0]["callback_data"] == "sh:add"


def test_handler_add_opens_time_picker(store):
    handler = hc.build_schedulehome_handler(store, lambda c, cid: "ok")
    text, markup = handler("add", "chat1")
    assert "設定時間" in text
    cbs = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert any(cb.endswith(":ok") for cb in cbs)


def test_handler_run_executes(store):
    store.add(label="x", time="07:00", days=["mon"], commands=["/music a"])
    handler = hc.build_schedulehome_handler(store, lambda c, cid: f"ran {c}")
    reply = handler("run sch_001", "chat1")
    assert "ran /music a" in reply


def test_handler_on_off_delete(store):
    store.add(time="07:00", days=["mon"])
    handler = hc.build_schedulehome_handler(store, lambda c, cid: "ok")
    assert "已停用" in handler("off sch_001", "chat1")
    assert store.get("sch_001")["enabled"] is False
    assert "已啟用" in handler("on sch_001", "chat1")
    assert "已刪除" in handler("delete sch_001", "chat1")


def test_callback_time_picker_adjust_and_next(store):
    cb = hc.build_schedulehome_callback_handler(store, lambda c, cid: "ok")
    # +1 hour from 07:00 → 08:00 picker.
    toast, text, markup = cb("t:07:00:h+", "", "chat1")
    assert "08:00" in text
    # next → recurrence picker.
    toast, text, markup = cb("t:08:00:ok", "", "chat1")
    assert "重複日" in text


def test_callback_recurrence_ok_creates_and_captures(store):
    cb = hc.build_schedulehome_callback_handler(store, lambda c, cid: "ok")
    # Choose weekday preset, then confirm.
    toast, text, markup = cb("r:07:30:0000000:wk", "", "chat1")
    assert "平日" in text
    toast, text, markup = cb("r:07:30:1111100:ok", "", "chat1")
    assert toast == "已建立排程"
    entry = store.get("sch_001")
    assert entry["schedule"] == {"time": "07:30", "days": hs.WEEKDAY_KEYS}
    # Capture mode now active for this chat.
    assert store.capture_target("chat1") == "sch_001"


def test_callback_recurrence_ok_requires_a_day(store):
    cb = hc.build_schedulehome_callback_handler(store, lambda c, cid: "ok")
    toast, text, markup = cb("r:07:30:0000000:ok", "", "chat1")
    assert toast == "請至少選擇一天"
    assert store.list() == []


def test_callback_day_toggle(store):
    cb = hc.build_schedulehome_callback_handler(store, lambda c, cid: "ok")
    # Toggle Monday (index 0) on.
    toast, text, markup = cb("r:07:30:0000000:d0", "", "chat1")
    assert "一" in text
    day_buttons = markup["inline_keyboard"][0]
    assert day_buttons[0]["text"].startswith("✅")


def test_callback_run_executes_and_keeps_list(store):
    store.add(label="x", time="07:00", days=["mon"], commands=["/music a"])
    cb = hc.build_schedulehome_callback_handler(store, lambda c, cid: f"ran {c}")
    toast, text, markup = cb("run:sch_001", "", "chat1")
    assert toast == "已執行"
    assert "ran /music a" in text
    assert markup["inline_keyboard"][-1][0]["callback_data"] == "sh:add"


def test_callback_cancel_ends_capture(store):
    store.add(time="07:00", days=["mon"])
    store.begin_capture("chat1", "sch_001")
    cb = hc.build_schedulehome_callback_handler(store, lambda c, cid: "ok")
    toast, text, markup = cb("cancel", "", "chat1")
    assert toast == "已取消"
    assert store.capture_target("chat1") is None


# --- edit flow (re-set time / recurrence, keep commands) -------------------
def test_edit_session_lifecycle(store):
    store.begin_edit("chat1", "sch_001")
    assert store.edit_target("chat1") == "sch_001"
    assert store.end_edit("chat1") == "sch_001"
    assert store.edit_target("chat1") is None


def test_list_has_edit_button(store):
    store.add(label="x", time="07:00", days=["mon"])
    _text, markup = hc.render_list(store)
    cbs = {b["callback_data"] for row in markup["inline_keyboard"] for b in row}
    assert "sh:edit:sch_001" in cbs


def test_callback_edit_opens_menu(store):
    store.add(label="x", time="08:30", days=["mon", "wed"], commands=["/music a"])
    cb = hc.build_schedulehome_callback_handler(store, lambda c, cid: "ok")
    toast, text, markup = cb("edit:sch_001", "", "chat1")
    # ✏️ now opens a sub-menu, not the time picker directly.
    labels = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert "sh:edittime:sch_001" in labels
    assert "sh:rename:sch_001" in labels
    assert store.edit_target("chat1") is None  # not in edit mode yet


def test_callback_edittime_opens_time_picker_seeded(store):
    store.add(label="x", time="08:30", days=["mon", "wed"], commands=["/music a"])
    cb = hc.build_schedulehome_callback_handler(store, lambda c, cid: "ok")
    toast, text, markup = cb("edittime:sch_001", "", "chat1")
    assert "08:30" in text  # picker seeded with the schedule's current time
    assert store.edit_target("chat1") == "sch_001"


def test_callback_rename_enters_capture(store):
    store.add(label="x", time="08:30", days=["mon"], commands=["/music a"])
    cb = hc.build_schedulehome_callback_handler(store, lambda c, cid: "ok")
    toast, text, markup = cb("rename:sch_001", "", "chat1")
    assert store.rename_target("chat1") == "sch_001"
    assert "新的名稱" in text


def test_callback_cancel_ends_rename(store):
    store.add(time="07:00", days=["mon"])
    store.begin_rename("chat1", "sch_001")
    cb = hc.build_schedulehome_callback_handler(store, lambda c, cid: "ok")
    cb("cancel", "", "chat1")
    assert store.rename_target("chat1") is None


def test_set_label_and_rename_session(store):
    store.add(label="old", time="07:00", days=["mon"])
    assert store.set_label("sch_001", "早安鬧鐘")["label"] == "早安鬧鐘"
    store.begin_rename("chat1", "sch_001")
    assert store.rename_target("chat1") == "sch_001"
    assert store.end_rename("chat1") == "sch_001"
    assert store.rename_target("chat1") is None


def test_delete_clears_rename(store):
    store.add(time="07:00", days=["mon"])
    store.begin_rename("chat1", "sch_001")
    store.delete("sch_001")
    assert store.rename_target("chat1") is None


def test_callback_edit_seeds_recurrence_from_existing_days(store):
    store.add(label="x", time="08:30", days=["mon", "wed"], commands=["/music a"])
    cb = hc.build_schedulehome_callback_handler(store, lambda c, cid: "ok")
    cb("edittime:sch_001", "", "chat1")
    # Stepping to the recurrence picker must pre-tick Mon+Wed.
    toast, text, markup = cb("t:08:30:ok", "", "chat1")
    day_buttons = markup["inline_keyboard"][0]
    assert day_buttons[0]["text"].startswith("✅")  # Mon
    assert day_buttons[2]["text"].startswith("✅")  # Wed
    assert not day_buttons[1]["text"].startswith("✅")  # Tue off


def test_callback_edit_updates_in_place_keeps_commands(store):
    store.add(label="x", time="08:30", days=["mon"], commands=["/music a", "/say hi"])
    cb = hc.build_schedulehome_callback_handler(store, lambda c, cid: "ok")
    cb("edittime:sch_001", "", "chat1")
    toast, text, markup = cb("r:09:15:1111100:ok", "", "chat1")
    assert toast == "已更新排程"
    entry = store.get("sch_001")
    assert entry["schedule"] == {"time": "09:15", "days": hs.WEEKDAY_KEYS}
    assert entry["commands"] == ["/music a", "/say hi"]  # commands untouched
    assert store.edit_target("chat1") is None  # edit session ended
    assert store.capture_target("chat1") is None  # not put into capture mode
    # No new schedule was created.
    assert [e["id"] for e in store.list()] == ["sch_001"]


def test_callback_cancel_ends_edit(store):
    store.add(time="07:00", days=["mon"])
    store.begin_edit("chat1", "sch_001")
    cb = hc.build_schedulehome_callback_handler(store, lambda c, cid: "ok")
    toast, text, markup = cb("cancel", "", "chat1")
    assert toast == "已取消"
    assert store.edit_target("chat1") is None


def test_delete_clears_edit(store):
    store.add(time="07:00", days=["mon"])
    store.begin_edit("chat1", "sch_001")
    store.delete("sch_001")
    assert store.edit_target("chat1") is None


def test_parse_time_fallback():
    assert hc._parse_time("23:45") == (23, 45)
    assert hc._parse_time("") == (7, 0)
    assert hc._parse_time(None) == (7, 0)
    assert hc._parse_time("99:99") == (7, 0)
