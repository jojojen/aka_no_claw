"""Stable compatibility facade for the `/research` pipeline (R3.9)."""

from __future__ import annotations

import sys

from .research import service as _service

# Keep the established module-level seams for callers and deterministic tests.
sys.modules[__name__] = _service
