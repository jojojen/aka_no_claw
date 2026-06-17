"""Run a Playwright scrape in a throwaway subprocess with a hard deadline.

The research market scrapes drive headless Chromium via Playwright's *sync*
API. Playwright's per-operation timeouts (goto/wait_for_selector) do NOT cover
teardown: when the browser process dies, ``browser.close()`` and the sync
driver transport can block forever. In-thread that wedges the whole job with no
timeout able to reach it.

Running each scrape in its own process group lets us SIGKILL the entire tree —
including the stuck Chromium grandchildren — on deadline, so nothing leaks.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys

logger = logging.getLogger(__name__)

_WORKER_MODULE = "openclaw_adapter.scrape_worker"


def run_in_subprocess(target: str, payload: dict, *, timeout: float) -> object:
    """Dispatch *target* to the scrape worker subprocess and return its result.

    Raises ``TimeoutError`` on deadline (after SIGKILLing the process group) and
    ``RuntimeError`` on worker failure/garbled output, so callers can catch and
    degrade the stage gracefully.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", _WORKER_MODULE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # own process group → killpg reaps chromium too
        env=os.environ.copy(),
    )
    request = json.dumps({"target": target, "payload": payload})
    try:
        out, err = proc.communicate(request, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        raise TimeoutError(f"scrape '{target}' exceeded {timeout}s") from None

    _forward_worker_logs(target, err)

    if proc.returncode != 0:
        raise RuntimeError(
            f"scrape worker '{target}' exited {proc.returncode}: {(err or '').strip()[:500]}"
        )
    try:
        response = json.loads(out)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(
            f"scrape worker '{target}' returned unparseable output: {(out or '')[:300]!r}"
        ) from exc
    if not response.get("ok"):
        raise RuntimeError(f"scrape '{target}' failed: {response.get('error')}")
    return response.get("result")


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - best effort reap
        pass


def _forward_worker_logs(target: str, stderr_text: str | None) -> None:
    """Surface the worker's captured logs in the parent log for traceability."""
    for line in (stderr_text or "").splitlines():
        line = line.rstrip()
        if line:
            logger.debug("[scrape:%s] %s", target, line)
