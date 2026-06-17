from __future__ import annotations

import json
import os
import textwrap
import time
from pathlib import Path

import pytest

from openclaw_adapter import research_command as rc
from openclaw_adapter import scrape_subprocess as ss
from openclaw_adapter import scrape_worker as sw


def _install_stub_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> str:
    """Write a throwaway worker module and make run_in_subprocess target it."""
    module_name = "stub_scrape_worker"
    (tmp_path / f"{module_name}.py").write_text(textwrap.dedent(body))
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", f"{tmp_path}{os.pathsep}{existing}" if existing else str(tmp_path))
    monkeypatch.setattr(ss, "_WORKER_MODULE", module_name)
    return module_name


def test_run_in_subprocess_returns_worker_result(tmp_path, monkeypatch):
    _install_stub_worker(
        tmp_path,
        monkeypatch,
        """
        import json, sys
        req = json.loads(sys.stdin.read())
        sys.stdout.write(json.dumps({"ok": True, "result": {"echo": req["payload"]}}))
        """,
    )
    out = ss.run_in_subprocess("active", {"query": "pikachu"}, timeout=10)
    assert out == {"echo": {"query": "pikachu"}}


def test_run_in_subprocess_raises_on_worker_error(tmp_path, monkeypatch):
    _install_stub_worker(
        tmp_path,
        monkeypatch,
        """
        import json, sys
        sys.stdin.read()
        sys.stdout.write(json.dumps({"ok": False, "error": "RuntimeError: boom"}))
        """,
    )
    with pytest.raises(RuntimeError, match="boom"):
        ss.run_in_subprocess("sold", {"query": "x"}, timeout=10)


def test_run_in_subprocess_times_out_and_kills_group(tmp_path, monkeypatch):
    """A worker that wedges forever must raise TimeoutError fast and leave no
    surviving process group (the chromium-leak guarantee)."""
    pid_file = tmp_path / "child.pid"
    _install_stub_worker(
        tmp_path,
        monkeypatch,
        f"""
        import os, sys, time
        open({str(pid_file)!r}, "w").write(str(os.getpid()))
        sys.stdin.read()
        time.sleep(120)
        """,
    )
    start = time.perf_counter()
    with pytest.raises(TimeoutError):
        ss.run_in_subprocess("active", {"query": "x"}, timeout=2)
    elapsed = time.perf_counter() - start
    assert elapsed < 15  # killed promptly, not waiting out sleep(120)

    child_pid = int(pid_file.read_text())
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)  # process gone → group was reaped


def test_worker_dispatch_active(monkeypatch):
    monkeypatch.setattr(
        rc, "_active_market_scrape_impl",
        lambda q, cap, n: [{"source": "mercari", "title": q, "price_jpy": cap, "n": n}],
    )
    out = sw._dispatch("active", {"query": "abc", "price_cap": 500, "max_results": 8})
    assert out == [{"source": "mercari", "title": "abc", "price_jpy": 500, "n": 8}]


def test_worker_dispatch_shop_reference_serializes(monkeypatch):
    ref = rc.ShopReference(
        label="遊々亭", buy_reference=100, sell_reference=200, stock_total=3,
        buy_count=1, sell_count=2, sample_urls=("u1", "u2"),
    )
    monkeypatch.setattr(rc, "_shop_reference_scrape_impl", lambda q, cap, opts: ref)
    out = sw._dispatch("shop_reference", {"query": "q", "price_cap": 9, "source_options": None})
    assert out["sample_urls"] == ["u1", "u2"]
    assert rc._shop_reference_from_dict(out).buy_reference == 100


def test_worker_dispatch_shop_reference_none(monkeypatch):
    monkeypatch.setattr(rc, "_shop_reference_scrape_impl", lambda q, cap, opts: None)
    assert sw._dispatch("shop_reference", {"query": "q", "price_cap": 9}) is None


def test_worker_dispatch_unknown_target():
    with pytest.raises(ValueError, match="unknown scrape target"):
        sw._dispatch("nope", {})


def test_isolated_thread_timeout_raises():
    with pytest.raises(TimeoutError, match="exceeded"):
        rc._run_in_isolated_thread(lambda: time.sleep(5), timeout=0.2)


def test_isolated_thread_returns_value_within_budget():
    assert rc._run_in_isolated_thread(lambda: 42, timeout=5) == 42
