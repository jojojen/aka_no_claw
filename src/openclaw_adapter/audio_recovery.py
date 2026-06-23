"""Recover from the macOS CoreAudio output wedge (``-66681`` / "AudioQueueStart
failed") by restarting the ``coreaudiod`` daemon.

afplay intermittently dies on startup with ``-66681`` even though the output
device is otherwise fine. Re-selecting the (single) output device does NOT clear
it — verified live — so the only reliable clear is restarting coreaudiod, which
launchd respawns within ~1s. That needs root, so the Mac mini's sudoers must
grant a NOPASSWD exception for exactly this command:

    jen ALL=(root) NOPASSWD: /usr/bin/killall coreaudiod

We invoke it with ``sudo -n`` (non-interactive): when the grant is absent the
call fails fast instead of hanging on a password prompt, and the caller reports
the real playback failure rather than silently faking success.
"""

from __future__ import annotations

import logging
import subprocess
import time

logger = logging.getLogger(__name__)

_KILLALL = "/usr/bin/killall"
_DAEMON = "coreaudiod"
_RESTART_TIMEOUT = 5
# launchd relaunches coreaudiod within ~1s; wait before the playback retry so
# the fresh daemon is ready to accept an audio queue.
_SETTLE_SECONDS = 1.5


def restart_coreaudiod(settle_seconds: float = _SETTLE_SECONDS) -> bool:
    """Best-effort restart of coreaudiod via passwordless sudo to clear a wedged
    audio output. Returns ``True`` only when the kill succeeds (then waits for
    the daemon to come back). Never raises and never blocks on a password prompt
    (``sudo -n``); returns ``False`` if the NOPASSWD grant is missing."""
    try:
        proc = subprocess.run(
            ["sudo", "-n", _KILLALL, _DAEMON],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_RESTART_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("coreaudiod restart could not run: %s", exc)
        return False
    if proc.returncode != 0:
        logger.warning(
            "coreaudiod restart denied (rc=%s): %s — is the NOPASSWD sudoers rule set?",
            proc.returncode,
            (proc.stderr or proc.stdout or "").strip()[:120],
        )
        return False
    time.sleep(settle_seconds)
    return True
