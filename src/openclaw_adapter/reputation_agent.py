from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from html import unescape
from pathlib import Path
from urllib.parse import urlencode
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_MERCARI_HOST = "jp.mercari.com"
_DEFAULT_SERVER_URL = "http://127.0.0.1:5000"
_DEFAULT_POLL_SECS = 5
_agent_thread_lock = threading.Lock()
_agent_thread: threading.Thread | None = None


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post(server_url: str, api_key: str, path: str, body: dict) -> dict:
    url = f"{server_url}{path}?token={api_key}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def _get(server_url: str, api_key: str, path: str) -> dict:
    url = f"{server_url}{path}?token={api_key}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())


# ── Playwright helpers ────────────────────────────────────────────────────────

def _capture_page(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector("body", timeout=15000)
    page.wait_for_timeout(2000)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    return {
        "raw_html":     page.content(),
        "visible_text": page.evaluate("() => document.body ? document.body.innerText : ''"),
    }


def _find_profile_url(html: str) -> str | None:
    for pat in [r'href="(/user/profile/([A-Za-z0-9_-]+))"',
                r'["\x27](/user/profile/([A-Za-z0-9_-]+))["\x27]']:
        m = re.search(pat, html)
        if m:
            return f"https://{_MERCARI_HOST}{unescape(m.group(1))}"
    return None


def _run_capture(query_url: str) -> dict:
    from playwright.sync_api import sync_playwright

    path = urlparse(query_url.strip()).path.rstrip("/")
    is_item = "/item/" in path

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            locale="ja-JP",
            viewport={"width": 1440, "height": 2200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        item_html = item_text = None
        profile_url = None
        query_kind = "profile"

        if is_item:
            pg = ctx.new_page()
            d = _capture_page(pg, f"https://{_MERCARI_HOST}{path}")
            item_html, item_text = d["raw_html"], d["visible_text"]
            pg.close()
            query_kind = "item"
            profile_url = _find_profile_url(item_html)
            if not profile_url:
                raise RuntimeError("Could not find seller profile in item page.")
        else:
            profile_url = f"https://{_MERCARI_HOST}{path}"

        pg = ctx.new_page()
        pg.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector("body", timeout=15000)
        pg.wait_for_timeout(2000)
        try:
            pg.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        profile_html = pg.content()
        profile_text = pg.evaluate("() => document.body ? document.body.innerText : ''")
        screenshot_bytes = pg.screenshot(full_page=True, type="png")
        pg.close()

        profile_id  = profile_url.rstrip("/").split("/")[-1]
        reviews_url = f"https://{_MERCARI_HOST}/user/reviews/{profile_id}"
        reviews_html = reviews_text = reviews_bad_text = None
        try:
            rpg = ctx.new_page()
            rd = _capture_page(rpg, reviews_url)
            reviews_html, reviews_text = rd["raw_html"], rd["visible_text"]
            try:
                bad_btn = rpg.query_selector('[aria-controls="bad"]')
                if bad_btn:
                    bad_btn.click()
                    rpg.wait_for_timeout(2000)
                    reviews_bad_text = rpg.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    )
            except Exception:
                pass
            rpg.close()
        except Exception as e:
            logger.warning("reputation_agent: reviews capture skipped: %s", e)

        ctx.close()
        browser.close()

    return {
        "query_kind":        query_kind,
        "profile_url":       profile_url,
        "profile_html":      profile_html,
        "profile_text":      profile_text,
        "screenshot_base64": base64.b64encode(screenshot_bytes).decode() if screenshot_bytes else None,
        "reviews_url":       reviews_url,
        "reviews_html":      reviews_html,
        "reviews_text":      reviews_text,
        "reviews_bad_text":  reviews_bad_text,
        "item_html":         item_html,
        "item_text":         item_text,
    }


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_agent_loop(
    server_url: str = _DEFAULT_SERVER_URL,
    api_key: str = "",
    poll_secs: int = _DEFAULT_POLL_SECS,
) -> None:
    """Blocking poll loop. Runs until KeyboardInterrupt or the thread is killed."""
    server_url = server_url.rstrip("/")
    logger.info("reputation_agent: starting — polling %s every %ss", server_url, poll_secs)

    while True:
        try:
            resp = _get(server_url, api_key, "/api/jobs/claim")
            job  = resp.get("job")

            if job is None:
                time.sleep(poll_secs)
                continue

            job_id    = job["job_id"]
            query_url = job["query_url"]
            logger.info("reputation_agent: [job %s] %s", job_id, query_url)

            try:
                result = _run_capture(query_url)
                out    = _post(server_url, api_key, f"/api/jobs/{job_id}/result", result)
                logger.info("reputation_agent: [job %s] done → %s%s",
                            job_id, server_url, out.get("proof_url", ""))
            except Exception as exc:
                logger.error("reputation_agent: [job %s] FAILED: %s", job_id, exc)
                try:
                    _post(server_url, api_key, f"/api/jobs/{job_id}/result", {"error": str(exc)})
                except Exception:
                    pass

        except KeyboardInterrupt:
            logger.info("reputation_agent: stopped.")
            break
        except Exception as e:
            logger.error("reputation_agent: [poll error] %s", e)
            time.sleep(poll_secs)


def start_agent_thread(
    server_url: str = _DEFAULT_SERVER_URL,
    api_key: str = "",
    poll_secs: int = _DEFAULT_POLL_SECS,
) -> threading.Thread:
    """Start the agent loop in a background daemon thread and return it.

    If a local agent thread is already alive in this process, it is reused.
    """
    global _agent_thread
    with _agent_thread_lock:
        if _agent_thread is not None and _agent_thread.is_alive():
            logger.debug("reputation_agent: reusing existing background thread")
            return _agent_thread
        t = threading.Thread(
            target=run_agent_loop,
            args=(server_url, api_key, poll_secs),
            daemon=True,
            name="reputation-agent",
        )
        t.start()
        _agent_thread = t
    logger.info("reputation_agent: background thread started (daemon)")
    return t


def check_prerequisites(api_key: str) -> str | None:
    """Return an error message if the agent cannot start, or None if OK."""
    if not api_key:
        return "REPUTATION_AGENT_ADMIN_TOKEN is not set"
    try:
        from pathlib import Path

        from playwright.sync_api import sync_playwright
    except ImportError:
        return "playwright is not installed — run: pip install playwright && playwright install chromium"
    try:
        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            if not executable.exists():
                return "Playwright Chromium is not installed — run: playwright install chromium"
    except Exception as exc:
        return f"Playwright runtime is not ready: {exc}"
    return None


def check_server_authorization(server_url: str, api_key: str) -> str | None:
    """Return an error message if the remote server rejects the admin token."""
    server_url = server_url.rstrip("/")
    query = urlencode({"token": api_key})
    url = f"{server_url}/admin?{query}"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                return f"reputation_snapshot admin check failed with HTTP {status}"
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return "REPUTATION_AGENT_ADMIN_TOKEN is invalid for REPUTATION_AGENT_SERVER_URL"
        return f"reputation_snapshot admin check failed with HTTP {exc.code}"
    except Exception as exc:
        return f"Could not reach reputation_snapshot server: {exc}"
    return None


def _is_local_server_url(server_url: str) -> bool:
    parsed = urlparse(server_url.rstrip("/"))
    return (parsed.hostname or "").lower() in {"127.0.0.1", "localhost"}


def _find_local_reputation_snapshot_dir() -> Path | None:
    workspace_root = Path(__file__).resolve().parents[3]
    candidate = workspace_root / "reputation_snapshot"
    if candidate.exists():
        return candidate
    return None


def _launch_local_reputation_snapshot(repo_dir: Path) -> None:
    start_script = repo_dir / "start.bat"
    if start_script.exists():
        subprocess.Popen(
            ["cmd", "/c", "start", "", "start.bat", "go"],
            cwd=str(repo_dir),
        )
        return

    python_exe = repo_dir / ".venv" / "Scripts" / "python.exe"
    app_file = repo_dir / "app.py"
    if python_exe.exists() and app_file.exists():
        subprocess.Popen(
            [str(python_exe), str(app_file)],
            cwd=str(repo_dir),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        return

    raise RuntimeError(f"Could not find a launcher for local reputation_snapshot at {repo_dir}")


def ensure_server_ready(server_url: str, api_key: str, *, timeout_seconds: float = 20.0) -> tuple[bool, str | None]:
    """Ensure the target reputation_snapshot server is reachable and accepts the token.

    Returns ``(server_started_now, error_message)``.
    """
    err = check_server_authorization(server_url, api_key)
    if err is None:
        return False, None
    if not _is_local_server_url(server_url):
        return False, err
    if "Could not reach reputation_snapshot server:" not in err:
        return False, err

    repo_dir = _find_local_reputation_snapshot_dir()
    if repo_dir is None:
        return False, err

    logger.info("reputation_agent: local server unreachable, attempting auto-start repo=%s", repo_dir)
    try:
        _launch_local_reputation_snapshot(repo_dir)
    except Exception as exc:
        return False, f"{err}. Local auto-start failed: {exc}"

    deadline = time.monotonic() + timeout_seconds
    last_err = err
    while time.monotonic() < deadline:
        time.sleep(1.0)
        last_err = check_server_authorization(server_url, api_key)
        if last_err is None:
            logger.info("reputation_agent: local reputation_snapshot server is now reachable")
            return True, None
        if "REPUTATION_AGENT_ADMIN_TOKEN is invalid" in last_err:
            return True, last_err

    return True, last_err


def ensure_agent_thread(
    server_url: str = _DEFAULT_SERVER_URL,
    api_key: str = "",
    poll_secs: int = _DEFAULT_POLL_SECS,
) -> tuple[threading.Thread, bool]:
    """Ensure a local reputation agent thread is alive.

    Returns ``(thread, started_now)``. Raises ``RuntimeError`` if prerequisites fail.
    """
    thread = _agent_thread
    if thread is not None and thread.is_alive():
        return thread, False

    err = check_prerequisites(api_key)
    if err:
        raise RuntimeError(err)
    _, err = ensure_server_ready(server_url, api_key)
    if err:
        raise RuntimeError(err)

    thread = start_agent_thread(server_url=server_url, api_key=api_key, poll_secs=poll_secs)
    return thread, True
