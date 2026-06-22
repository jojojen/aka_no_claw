#!/usr/bin/env python3
"""Size-capped rotating sink for a service's stdout/stderr.

The mac-mini-stack launcher used to redirect each background service straight to
``logs/<service>.log`` (``nohup ... >> file`` / ``launchctl submit -o/-e file``).
That path has no size cap, so a chatty DEBUG service (opportunity-agent) grew its
log to 1.3G in six weeks (issue #42). This helper reads stdin line-by-line and
writes through a ``RotatingFileHandler`` so every redirected service log is now
bounded by ``maxBytes * (backupCount + 1)``.

Usage:
    <service> 2>&1 | log_rotator.py logs/<service>.log

Caps are read from the environment so the launcher can tune them in one place:
    ROTATE_BYTES  per-file size cap in bytes        (default 50 MiB)
    ROTATE_KEEP   number of rotated backups to keep (default 5)
"""
from __future__ import annotations

import logging.handlers
import os
import sys
from pathlib import Path


def _positive_int(env_name: str, default: int) -> int:
    raw = os.getenv(env_name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: log_rotator.py <logfile>\n")
        return 2

    log_path = Path(argv[1])
    log_path.parent.mkdir(parents=True, exist_ok=True)

    max_bytes = _positive_int("ROTATE_BYTES", 50 * 1024 * 1024)
    backup_count = _positive_int("ROTATE_KEEP", 5)

    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    # The upstream stream already carries its own formatting/timestamps; emit each
    # line verbatim so the rotated file is byte-for-byte what the service printed.
    handler.setFormatter(logging.Formatter("%(message)s"))

    rotator = logging.getLogger("log_rotator")
    rotator.setLevel(logging.INFO)
    rotator.propagate = False
    rotator.addHandler(handler)

    for line in sys.stdin:
        rotator.info(line.rstrip("\n"))
        handler.flush()

    handler.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
