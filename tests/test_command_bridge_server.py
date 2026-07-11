"""Command-bridge transport envelope versioning (aka_no_claw#77 D2.4 follow-up).

Verifies _stamp_envelope_version stamps every JSON object response and NDJSON
stream event with envelope_version, mirroring reputation_snapshot's
after_request hook, without overriding an already-present value.
"""

from __future__ import annotations

from openclaw_adapter.command_bridge_server import (
    COMMAND_BRIDGE_ENVELOPE_VERSION,
    _stamp_envelope_version,
)


def test_stamps_envelope_version_when_absent() -> None:
    payload = {"status": "ok", "message": "hi"}
    stamped = _stamp_envelope_version(payload)
    assert stamped["envelope_version"] == COMMAND_BRIDGE_ENVELOPE_VERSION
    assert stamped["status"] == "ok"
    assert stamped["message"] == "hi"


def test_does_not_override_existing_envelope_version() -> None:
    payload = {"status": "ok", "envelope_version": 99}
    stamped = _stamp_envelope_version(payload)
    assert stamped["envelope_version"] == 99


def test_does_not_mutate_input_payload() -> None:
    payload = {"status": "ok"}
    _stamp_envelope_version(payload)
    assert "envelope_version" not in payload


def test_current_envelope_version_is_1() -> None:
    assert COMMAND_BRIDGE_ENVELOPE_VERSION == 1
