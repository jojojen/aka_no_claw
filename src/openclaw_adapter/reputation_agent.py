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


def _read_body_text(page) -> str:
    return page.evaluate("() => document.body ? document.body.innerText : ''")


def _click_review_tab(page, tab: str) -> bool:
    selector_map = {
        "good": ('[aria-controls="good"]', '[data-testid*="good"]'),
        "bad": ('[aria-controls="bad"]', '[data-testid*="bad"]'),
        "seller": ('[aria-controls="seller"]', '[aria-controls*="seller"]', '[data-testid*="seller"]'),
        "buyer": ('[aria-controls="buyer"]', '[aria-controls*="buyer"]', '[data-testid*="buyer"]'),
    }
    label_map = {
        "good": ("良かった", "良い"),
        "bad": ("残念だった", "悪い"),
        "seller": ("出品者",),
        "buyer": ("購入者",),
    }
    for selector in selector_map[tab]:
        try:
            element = page.query_selector(selector)
            if element:
                element.click()
                page.wait_for_timeout(1200)
                return True
        except Exception:
            pass
    for label in label_map[tab]:
        try:
            locator = page.get_by_role("tab", name=re.compile(label))
            if locator.count():
                locator.first.click()
                page.wait_for_timeout(1200)
                return True
        except Exception:
            pass
        try:
            locator = page.get_by_text(label, exact=False)
            if locator.count():
                locator.first.click()
                page.wait_for_timeout(1200)
                return True
        except Exception:
            pass
    return False


def _capture_review_tab_texts(page, initial_capture: dict) -> dict:
    tab_text = {
        "reviews_html": initial_capture["raw_html"],
        "reviews_text": initial_capture["visible_text"],
        "reviews_bad_text": "",
        "reviews_buyer_text": "",
        "reviews_buyer_bad_text": "",
    }

    if _click_review_tab(page, "seller"):
        _click_review_tab(page, "good")
        tab_text["reviews_html"] = page.content()
        tab_text["reviews_text"] = _read_body_text(page)

    if _click_review_tab(page, "bad"):
        tab_text["reviews_bad_text"] = _read_body_text(page)

    if _click_review_tab(page, "buyer"):
        _click_review_tab(page, "good")
        tab_text["reviews_buyer_text"] = _read_body_text(page)
        if _click_review_tab(page, "bad"):
            tab_text["reviews_buyer_bad_text"] = _read_body_text(page)

    return tab_text


def _profile_candidate_records(page) -> list[dict[str, str]]:
    try:
        candidates = page.eval_on_selector_all(
            "a",
            """
            (els) => els.map((e) => ({
              href: e.href || "",
              text: (e.innerText || "").trim(),
              location: e.getAttribute("data-location") || "",
              aria: e.getAttribute("aria-label") || "",
            }))
            """,
        )
    except Exception:
        return []

    return [candidate for candidate in candidates if isinstance(candidate, dict)]


def _select_profile_candidate(candidates: list[dict[str, str]]) -> dict[str, str] | None:
    prioritized: list[dict[str, str]] = []
    fallback: list[dict[str, str]] = []
    for candidate in candidates:
        href = str(candidate.get("href") or "").strip()
        if "/user/profile/" not in href:
            continue
        location = str(candidate.get("location") or "").strip().lower()
        text = str(candidate.get("text") or "").strip()
        aria = str(candidate.get("aria") or "").strip().lower()
        if "seller" in location or "seller" in aria or text:
            prioritized.append(candidate)
        else:
            fallback.append(candidate)
    return (prioritized + fallback)[0] if prioritized or fallback else None


def _find_profile_url(html: str) -> str | None:
    patterns = [
        rf'href=(?:"|\')(?P<url>https://{re.escape(_MERCARI_HOST)}/user/profile/[A-Za-z0-9_-]+)(?:"|\')',
        r'href=(?:"|\')(?P<url>/user/profile/[A-Za-z0-9_-]+)(?:"|\')',
        r'(?:"|\')(?P<url>/user/profile/[A-Za-z0-9_-]+)(?:"|\')',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            url = unescape(m.group("url"))
            if url.startswith("http://") or url.startswith("https://"):
                return url
            return f"https://{_MERCARI_HOST}{url}"
    return None


def _find_profile_url_in_page(page) -> str | None:
    candidate = _select_profile_candidate(_profile_candidate_records(page))
    if candidate is None:
        return None
    href = str(candidate.get("href") or "").strip()
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return f"https://{_MERCARI_HOST}{href}"
    return None


def _extract_first_integer(text: str) -> int | None:
    segments = [segment.strip() for segment in text.split(",") if segment.strip()]
    for segment in segments[1:] + segments[:1]:
        match = re.search(r"(?<![\d.])([\d][\d,]*)(?![\d.])", segment)
        if match:
            return int(match.group(1).replace(",", ""))
    return None


def _looks_like_person_name(value: str) -> bool:
    candidate = value.strip()
    if not candidate or len(candidate) > 80:
        return False
    if candidate.startswith("http"):
        return False
    if "<" in candidate or ">" in candidate:
        return False
    if re.fullmatch(r"[\d,\s]+", candidate):
        return False
    if _is_suspicious_seller_name(candidate):
        return False
    if any(token in candidate for token in ("メルカリについて", "会社概要", "運営会社")):
        return False
    return True


def _is_suspicious_seller_name(value: str) -> bool:
    return bool(re.fullmatch(r"(?:Seller Level|Quick shipment)(?:\s+\d+)?", value.strip()))


def _parse_item_seller_label(label: str) -> dict[str, object]:
    segments = [segment.strip() for segment in label.split(",") if segment.strip()]
    display_name = segments[0] if segments and _looks_like_person_name(segments[0]) else None
    return {
        "display_name": display_name,
        "seller_total_reviews": _extract_first_integer(label),
    }


def _extract_item_seller_from_text(visible_text: str) -> dict[str, object]:
    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in visible_text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)

    display_name = None
    total_reviews = None
    try:
        seller_index = lines.index("出品者")
    except ValueError:
        seller_index = -1
    seller_window = lines[seller_index + 1 : seller_index + 6] if seller_index >= 0 else lines[:12]

    for line in seller_window:
        combined_match = re.fullmatch(r"(.+?)\s+([\d,]+)", line)
        if combined_match and _looks_like_person_name(combined_match.group(1)):
            display_name = combined_match.group(1).strip()
            total_reviews = int(combined_match.group(2).replace(",", ""))
            break

    if display_name is None:
        for line in seller_window:
            if _looks_like_person_name(line):
                display_name = line
                break

    if total_reviews is None:
        for line in seller_window:
            if re.fullmatch(r"[\d,]+", line):
                total_reviews = int(line.replace(",", ""))
                break

    return {
        "display_name": display_name,
        "seller_total_reviews": total_reviews,
    }


def _extract_item_seller_context(page, item_html: str, item_text: str) -> dict[str, object]:
    context: dict[str, object] = {
        "profile_url": _find_profile_url(item_html),
        "display_name": None,
        "seller_total_reviews": None,
    }

    candidate = _select_profile_candidate(_profile_candidate_records(page))
    if candidate is not None:
        href = str(candidate.get("href") or "").strip()
        if href and context["profile_url"] is None:
            context["profile_url"] = href if href.startswith("http") else f"https://{_MERCARI_HOST}{href}"
        candidate_text = " , ".join(
            part for part in (str(candidate.get("aria") or "").strip(), str(candidate.get("text") or "").strip()) if part
        )
        parsed_candidate = _parse_item_seller_label(candidate_text)
        for key, value in parsed_candidate.items():
            if context.get(key) is None and value is not None:
                context[key] = value

    if context["profile_url"] is None:
        context["profile_url"] = _find_profile_url_in_page(page)

    parsed_label = _parse_item_seller_label(item_html)
    for key, value in parsed_label.items():
        if context.get(key) is None and value is not None:
            context[key] = value

    parsed_text = _extract_item_seller_from_text(item_text)
    for key, value in parsed_text.items():
        if value is None:
            continue
        if key == "display_name" and (
            context.get(key) is None or _is_suspicious_seller_name(str(context.get(key)))
        ):
            context[key] = value
        elif key == "seller_total_reviews" and (
            context.get(key) is None or int(value) > int(context.get(key) or 0)
        ):
            context[key] = value
    return context


def _read_profile_page_state(page, profile_id: str) -> dict[str, object]:
    try:
        snapshot = page.evaluate(
            """
            (profileId) => {
              const bodyText = document.body ? document.body.innerText : "";
              const hrefs = Array.from(document.querySelectorAll("a[href]")).map((e) => e.href || "");
              return {
                body_text: bodyText,
                has_heading: !!document.querySelector('[data-testid="mer-profile-heading"] h1, h1'),
                has_avatar: !!document.querySelector('img[src*="thumb/members/"]'),
                has_reviews_link: hrefs.some((href) => href.includes(`/user/reviews/${profileId}`)),
                has_profile_link: hrefs.some((href) => href.includes(`/user/profile/${profileId}`)),
              };
            }
            """,
            profile_id,
        )
    except Exception:
        return {}
    return snapshot if isinstance(snapshot, dict) else None


def _profile_page_loaded(snapshot: dict[str, object] | None) -> bool:
    if not isinstance(snapshot, dict):
        return False
    body_text = str(snapshot.get("body_text") or "")
    has_heading = bool(snapshot.get("has_heading"))
    has_avatar = bool(snapshot.get("has_avatar"))
    has_reviews_link = bool(snapshot.get("has_reviews_link"))
    has_profile_link = bool(snapshot.get("has_profile_link"))
    if has_avatar or has_reviews_link:
        return True
    if any(token in body_text for token in ("メルカリについて", "会社概要", "運営会社")) and not has_avatar:
        return False
    return has_heading and has_profile_link


def _capture_profile_page(page, profile_url: str) -> tuple[str, str, bytes]:
    page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector("body", timeout=15000)
    profile_id = profile_url.rstrip("/").split("/")[-1]

    for attempt in range(2):
        for selector in ('[data-testid="mer-profile-heading"]', 'img[src*="thumb/members/"]'):
            try:
                page.wait_for_selector(selector, timeout=4000)
                break
            except Exception:
                continue
        page.wait_for_timeout(1500)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        if _profile_page_loaded(_read_profile_page_state(page, profile_id)):
            return (
                page.content(),
                page.evaluate("() => document.body ? document.body.innerText : ''"),
                page.screenshot(full_page=True, type="png"),
            )
        if attempt == 0:
            page.reload(wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("body", timeout=15000)
    raise RuntimeError("Could not load seller profile content from Mercari.")


def _resolve_item_profile_url(page, item_html: str) -> str | None:
    profile_url = _find_profile_url(item_html)
    if profile_url:
        return profile_url

    for selector in ('a[data-location="item_details:seller_info"]', '[data-testid="seller-link"]'):
        try:
            page.wait_for_selector(selector, timeout=5000)
            break
        except Exception:
            continue

    page.wait_for_timeout(1500)
    profile_url = _find_profile_url(page.content())
    if profile_url:
        return profile_url
    return _find_profile_url_in_page(page)


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
        display_name = None
        seller_total_reviews = None
        query_kind = "profile"

        if is_item:
            pg = ctx.new_page()
            d = _capture_page(pg, f"https://{_MERCARI_HOST}{path}")
            item_html, item_text = d["raw_html"], d["visible_text"]
            query_kind = "item"
            seller_context = _extract_item_seller_context(pg, item_html, item_text)
            profile_url = str(seller_context.get("profile_url") or "") or _resolve_item_profile_url(pg, item_html)
            display_name = seller_context.get("display_name")
            seller_total_reviews = seller_context.get("seller_total_reviews")
            pg.close()
            if not profile_url:
                raise RuntimeError("Could not find seller profile in item page.")
        else:
            profile_url = f"https://{_MERCARI_HOST}{path}"

        pg = ctx.new_page()
        profile_html, profile_text, screenshot_bytes = _capture_profile_page(pg, profile_url)
        pg.close()

        profile_id  = profile_url.rstrip("/").split("/")[-1]
        reviews_url = f"https://{_MERCARI_HOST}/user/reviews/{profile_id}"
        reviews_html = reviews_text = reviews_bad_text = None
        reviews_buyer_text = reviews_buyer_bad_text = None
        try:
            rpg = ctx.new_page()
            rd = _capture_page(rpg, reviews_url)
            tab_text = _capture_review_tab_texts(rpg, rd)
            reviews_html = tab_text["reviews_html"]
            reviews_text = tab_text["reviews_text"]
            reviews_bad_text = tab_text["reviews_bad_text"]
            reviews_buyer_text = tab_text["reviews_buyer_text"]
            reviews_buyer_bad_text = tab_text["reviews_buyer_bad_text"]
            logger.info(
                "reputation_agent: reviews captured text_len=%d bad_len=%d buyer_len=%d buyer_bad_len=%d",
                len(reviews_text or ""),
                len(reviews_bad_text or ""),
                len(reviews_buyer_text or ""),
                len(reviews_buyer_bad_text or ""),
            )
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
        "reviews_buyer_text":     reviews_buyer_text,
        "reviews_buyer_bad_text": reviews_buyer_bad_text,
        "item_html":         item_html,
        "item_text":         item_text,
        "display_name":      display_name,
        "seller_total_reviews": seller_total_reviews,
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
    no_browser_script = repo_dir / "start-no-browser.bat"
    if no_browser_script.exists():
        subprocess.Popen(
            ["cmd", "/c", "start", "", "start-no-browser.bat"],
            cwd=str(repo_dir),
        )
        return

    start_script = repo_dir / "start.bat"
    if start_script.exists():
        subprocess.Popen(
            ["cmd", "/c", "start", "", "start.bat", "go", "--no-browser"],
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
