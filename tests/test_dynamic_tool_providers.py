from __future__ import annotations

import pytest

from openclaw_adapter.dynamic_tools.providers import (
    DeterministicFailureProvider,
    ProviderUnavailable,
    is_truncation_error,
)


def test_deterministic_failure_provider_never_contacts_a_backend() -> None:
    with pytest.raises(ProviderUnavailable, match="offline"):
        DeterministicFailureProvider(reason="offline").generate("request")


def test_truncation_classifier_is_explicit_about_its_marker_set() -> None:
    assert is_truncation_error("unexpected EOF", ("unexpected eof",))
    assert not is_truncation_error("HTTP 503", ("unexpected eof",))
