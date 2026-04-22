from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from html import unescape
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_MERCARI_HOST = "jp.mercari.com"
_DEFAULT_SERVER_URL = "https://reputation-snapshot.fly.dev"
_DEFAULT_POLL_SECS = 5


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
    """Start the agent loop in a background daemon thread and return it."""
    t = threading.Thread(
        target=run_agent_loop,
        args=(server_url, api_key, poll_secs),
        daemon=True,
        name="reputation-agent",
    )
    t.start()
    logger.info("reputation_agent: background thread started (daemon)")
    return t


def check_prerequisites(api_key: str) -> str | None:
    """Return an error message if the agent cannot start, or None if OK."""
    if not api_key:
        return "REPUTATION_AGENT_ADMIN_TOKEN is not set"
    try:
        from playwright.sync_api import sync_playwright as _  # noqa: F401
    except ImportError:
        return "playwright is not installed — run: pip install playwright && playwright install chromium"
    return None
