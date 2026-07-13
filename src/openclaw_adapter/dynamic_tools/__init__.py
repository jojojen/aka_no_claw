"""Stable compatibility facade for the dynamic-tool pipeline (R4.8)."""

from __future__ import annotations

import sys
from pathlib import Path

from . import service as _service

# Preserve module-level dependency seams used by production adapters and tests.
_service.__path__ = [str(Path(__file__).parent)]
sys.modules[__name__] = _service
