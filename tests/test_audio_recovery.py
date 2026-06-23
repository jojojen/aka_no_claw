"""Tests for restart_coreaudiod — the passwordless-sudo recovery for the macOS
CoreAudio output wedge (-66681). The subprocess is stubbed so these assert the
success/denied/error contract without touching the real daemon or sudo."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

from openclaw_adapter import audio_recovery as ar


def test_restart_success_waits_and_returns_true(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        ar.subprocess, "run",
        lambda args, **k: calls.append(args) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    slept: list[float] = []
    monkeypatch.setattr(ar.time, "sleep", lambda s: slept.append(s))
    assert ar.restart_coreaudiod(settle_seconds=0.2) is True
    assert calls[0] == ["sudo", "-n", ar._KILLALL, ar._DAEMON]  # non-interactive sudo
    assert slept == [0.2]  # waited for the daemon to come back


def test_restart_denied_returns_false_without_sleeping(monkeypatch):
    # NOPASSWD grant missing → sudo -n exits non-zero; must report failure, not hang.
    monkeypatch.setattr(
        ar.subprocess, "run",
        lambda args, **k: SimpleNamespace(returncode=1, stdout="", stderr="a password is required"),
    )
    slept: list[float] = []
    monkeypatch.setattr(ar.time, "sleep", lambda s: slept.append(s))
    assert ar.restart_coreaudiod() is False
    assert slept == []  # no settle wait when the restart never happened


def test_restart_returns_false_on_oserror(monkeypatch):
    def boom(*a, **k):
        raise OSError("sudo missing")

    monkeypatch.setattr(ar.subprocess, "run", boom)
    assert ar.restart_coreaudiod() is False


def test_restart_returns_false_on_timeout(monkeypatch):
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="sudo", timeout=5)

    monkeypatch.setattr(ar.subprocess, "run", timeout)
    assert ar.restart_coreaudiod() is False
