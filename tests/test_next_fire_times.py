"""Tests for next_fire_times function in home_schedule.py."""
from __future__ import annotations

from datetime import datetime

import pytest

from openclaw_adapter import home_schedule as hs
from openclaw_adapter import home_schedule_command as hc


@pytest.fixture
def store(tmp_path):
    return hs.HomeScheduleStore(str(tmp_path / "home_schedules.json"))


# --- next_fire_times tests ------------------------------------------------
def test_next_fire_times_invalid_time_returns_empty(store):
    entry = store.add(time="", days=["mon"])
    result = hs.next_fire_times(entry, datetime(2026, 6, 22, 7, 0))
    assert result == []


def test_next_fire_times_disabled_entry_returns_empty(store):
    entry = store.add(time="07:30", days=["mon"], enabled=False)
    result = hs.next_fire_times(entry, datetime(2026, 6, 22, 6, 0))
    assert result == []


def test_next_fire_times_empty_days_returns_empty(store):
    entry = store.add(time="07:30", days=[])
    result = hs.next_fire_times(entry, datetime(2026, 6, 22, 7, 0))
    assert result == []


def test_next_fire_times_time_not_passed_today_counts_today(store):
    # 2026-06-22 is Monday. Time is 09:00, now is 07:00.
    entry = store.add(time="09:00", days=["mon"])
    result = hs.next_fire_times(entry, datetime(2026, 6, 22, 7, 0), count=1)
    assert len(result) == 1
    assert result[0] == datetime(2026, 6, 22, 9, 0)


def test_next_fire_times_time_already_passed_skips_today(store):
    # 2026-06-22 is Monday. Time is 07:00, now is 09:00.
    entry = store.add(time="07:00", days=["mon"])
    result = hs.next_fire_times(entry, datetime(2026, 6, 22, 9, 0), count=1)
    assert len(result) == 1
    # Next Monday after 2026-06-22 is 2026-06-29.
    assert result[0] == datetime(2026, 6, 29, 7, 0)


def test_next_fire_times_time_exactly_now_skips_today(store):
    # 2026-06-22 is Monday. Time is exactly now.
    entry = store.add(time="09:00", days=["mon"])
    result = hs.next_fire_times(entry, datetime(2026, 6, 22, 9, 0), count=1)
    assert len(result) == 1
    # Should skip to next Monday.
    assert result[0] == datetime(2026, 6, 29, 9, 0)


def test_next_fire_times_specific_days_list(store):
    # Only Mon and Fri, starting Thursday 2026-06-25.
    entry = store.add(time="10:00", days=["mon", "fri"])
    result = hs.next_fire_times(entry, datetime(2026, 6, 25, 9, 0), count=3)
    assert len(result) == 3
    # Next Fri: 2026-06-26, then Mon: 2026-06-29, then Fri: 2026-07-03.
    assert result[0] == datetime(2026, 6, 26, 10, 0)
    assert result[1] == datetime(2026, 6, 29, 10, 0)
    assert result[2] == datetime(2026, 7, 3, 10, 0)


def test_next_fire_times_returns_requested_count(store):
    entry = store.add(time="08:00", days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    result = hs.next_fire_times(entry, datetime(2026, 6, 22, 7, 0), count=5)
    assert len(result) == 5


def test_next_fire_times_default_count_is_3(store):
    entry = store.add(time="08:00", days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    result = hs.next_fire_times(entry, datetime(2026, 6, 22, 7, 0))
    assert len(result) == 3


def test_next_fire_times_preserves_tzinfo(store):
    from datetime import timezone
    tz = timezone.utc
    now = datetime(2026, 6, 22, 7, 0, tzinfo=tz)
    entry = store.add(time="09:00", days=["mon", "tue"])
    result = hs.next_fire_times(entry, now, count=1)
    assert result[0].tzinfo == tz


def test_next_fire_times_bounded_scan(store):
    # Only one matching day in the 15-day window.
    entry = store.add(time="08:00", days=["mon"])
    # Start on Tuesday 2026-06-23, next Monday is 2026-06-29 (6 days away, within 15-day limit).
    result = hs.next_fire_times(entry, datetime(2026, 6, 23, 7, 0), count=2)
    assert len(result) == 2
    assert result[0] == datetime(2026, 6, 29, 8, 0)
    assert result[1] == datetime(2026, 7, 6, 8, 0)


def test_next_fire_times_weekday_keys_normalized(store):
    # Pass days in reverse/duplicate order; should normalize.
    entry = store.add(time="08:00", days=["fri", "mon", "mon"])
    result = hs.next_fire_times(entry, datetime(2026, 6, 22, 7, 0), count=2)
    assert len(result) == 2
    # Monday 2026-06-22 at 08:00.
    assert result[0] == datetime(2026, 6, 22, 8, 0)
    # Friday 2026-06-26 at 08:00.
    assert result[1] == datetime(2026, 6, 26, 8, 0)


# --- command rendering tests -----------------------------------------------
def test_render_list_shows_fire_times_enabled(store):
    store.add(label="morning", time="07:30", days=["mon", "wed", "fri"], enabled=True)
    text, markup = hc.render_list(store)
    assert "⏭ 接下來：" in text


def test_render_list_shows_stopped_for_disabled(store):
    store.add(label="paused", time="07:30", days=["mon"], enabled=False)
    text, markup = hc.render_list(store)
    assert "⏭ 已停用" in text


def test_render_list_no_fire_line_for_empty_days(store):
    store.add(label="broken", time="07:30", days=[], enabled=True)
    text, markup = hc.render_list(store)
    # No fire line should appear for empty days.
    assert "⏭ 接下來：" not in text


def test_render_edit_menu_shows_fire_times_enabled(store):
    entry = store.add(label="test", time="09:00", days=["tue", "thu"], enabled=True)
    text, markup = hc.render_edit_menu(entry)
    assert "⏭ 接下來：" in text


def test_render_edit_menu_shows_stopped_for_disabled(store):
    entry = store.add(label="test", time="09:00", days=["tue"], enabled=False)
    text, markup = hc.render_edit_menu(entry)
    assert "⏭ 已停用" in text


def test_capture_hint_shows_fire_times_enabled(store):
    entry = store.add(label="new", time="08:00", days=["mon", "tue", "wed"])
    hint = hc._capture_hint(entry)
    assert "⏭ 接下來：" in hint


def test_format_next_fire_times_locale(store):
    entry = store.add(time="10:00", days=["mon", "wed"])
    times = hs.next_fire_times(entry, datetime(2026, 6, 22, 9, 0), count=2)
    formatted = hc._format_next_fire_times(times)
    # Should include month/day, weekday char, and time.
    assert "06/22" in formatted
    assert "一" in formatted
    assert "10:00" in formatted


def test_format_next_fire_times_empty(store):
    formatted = hc._format_next_fire_times([])
    assert formatted == ""
