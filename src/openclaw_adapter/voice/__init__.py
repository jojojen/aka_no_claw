"""Voice-intent gating for the web command bridge (issue #82).

PR1 scope: voice provenance, minimal action registry, first-use
unresolved-control gate before open-ended chat tools, clarification
contract. Embedding / prototype learning arrive in later PRs — see
docs/VOICE_CONTROL_PERSONALIZATION_DESIGN.md (canonical design).
"""

from .action_registry import CompositeVoiceActionRegistry
from .intent_gate import VoiceIntentGate
from .models import (
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    VoiceActionCandidate,
    VoiceActionDescriptor,
    VoiceActionRegistry,
    VoiceClarification,
    VoiceUserContext,
)

__all__ = [
    "RISK_HIGH",
    "RISK_LOW",
    "RISK_MEDIUM",
    "CompositeVoiceActionRegistry",
    "VoiceActionCandidate",
    "VoiceActionDescriptor",
    "VoiceActionRegistry",
    "VoiceClarification",
    "VoiceIntentGate",
    "VoiceUserContext",
]
