"""Shared pytest fixtures for the aka_no_claw suite.

Host-budget isolation (#22/#24): the host budget defaults to a shared SQLite
file in the system temp dir so every live OpenClaw process coordinates through
one store. Tests must NOT touch that live file — a test tripping a cooldown
there would make the real /research worker back off. This autouse fixture points
``OPENCLAW_HOST_BUDGET_DB`` at a per-test temp path and resets the process-wide
singleton so each test gets a clean, isolated budget.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_host_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_HOST_BUDGET_DB", str(tmp_path / "host_budget.sqlite3"))
    try:
        from market_monitor.host_budget import reset_host_budget
    except Exception:
        yield
        return
    reset_host_budget()
    try:
        from market_monitor.http import reset_circuit_breaker
    except Exception:
        reset_circuit_breaker = None
    if reset_circuit_breaker is not None:
        reset_circuit_breaker()
    try:
        yield
    finally:
        reset_host_budget()
        if reset_circuit_breaker is not None:
            reset_circuit_breaker()
