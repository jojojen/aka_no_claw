#!/usr/bin/env python3
"""Live certification probe for vision-pool providers (WP-3).

Builds a deterministic probe image in code (Pillow 200×200 half-red/half-blue
with digit "7"), sends it to each (provider, model) candidate, and prints the
result so a human/agent can judge whether the reply reflects the image.

Usage:
    .venv/bin/python scripts/probe_vision_providers.py

Environment (sourced from .env or already loaded by dotenv):
    GEMINI_API_KEY
    MISTRAL_API_KEY
    OPENCLAW_LOCAL_VISION_ENDPOINT  (default http://127.0.0.1:11434)
    OPENCLAW_LOCAL_VISION_MODEL     (default qwen2.5vl)
    OPENCLAW_LOCAL_VISION_TIMEOUT_SECONDS (default 120)
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _build_probe_image() -> str:
    """200×200, left half red / right half blue, white digit '7' in center."""
    from PIL import Image, ImageDraw, ImageFont

    im = Image.new("RGB", (200, 200))
    draw = ImageDraw.Draw(im)
    draw.rectangle([(0, 0), (99, 199)], fill=(255, 0, 0))
    draw.rectangle([(100, 0), (199, 199)], fill=(0, 0, 255))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 80)
    except (IOError, OSError):
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), "7", font=font)
    x = (200 - (bbox[2] - bbox[0])) // 2
    y = (200 - (bbox[3] - bbox[1])) // 2
    draw.text((x, y), "7", fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    import base64
    return base64.b64encode(buf.getvalue()).decode("ascii")


CANDIDATES: list[dict[str, object]] = [
    {"provider": "gemini", "model": "gemini-2.5-flash", "key_var": "GEMINI_API_KEY"},
    {"provider": "gemini", "model": "gemini-2.5-pro", "key_var": "GEMINI_API_KEY"},
    {"provider": "mistral", "model": "pixtral-12b-latest", "key_var": "MISTRAL_API_KEY"},
    {"provider": "mistral", "model": "pixtral-large-2503", "key_var": "MISTRAL_API_KEY"},
    {"provider": "local", "model": os.environ.get("OPENCLAW_LOCAL_VISION_MODEL", "qwen2.5vl"),
     "endpoint": os.environ.get("OPENCLAW_LOCAL_VISION_ENDPOINT", "http://127.0.0.1:11434")},
]


def _probe_gemini(api_key: str, model: str, image_b64: str) -> str:
    payload = {
        "contents": [{"role": "user", "parts": [
            {"text": "describe the colors and the digit in this image, one line."},
            {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
        ]}],
        "generationConfig": {"temperature": 0},
    }
    from urllib.parse import quote
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model, safe='')}:generateContent"
        f"?key={quote(api_key, safe='')}"
    )
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    parts = []
    for c in body.get("candidates") or []:
        for p in (c.get("content") or {}).get("parts") or []:
            if isinstance(p.get("text"), str):
                parts.append(p["text"])
    return "".join(parts).strip()


def _probe_mistral(api_key: str, model: str, image_b64: str) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "describe the colors and the digit in this image, one line."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]}],
        "temperature": 0,
    }
    url = "https://api.mistral.ai/v1/chat/completions"
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return (body.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()


def _probe_local(endpoint: str, model: str, image_b64: str) -> str:
    payload = {
        "model": model,
        "prompt": "describe the colors and the digit in this image, one line.",
        "images": [image_b64],
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
    }
    url = f"{endpoint.rstrip('/')}/api/generate"
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return (body.get("response") or "").strip()


def main() -> int:
    print("=" * 60)
    print("Vision Provider Certification Probe (WP-3)")
    print("=" * 60)
    print()

    image_b64 = _build_probe_image()
    print(f"Probe image: 200×200 half-red/half-blue with digit '7'")
    print()

    results: list[dict[str, object]] = []

    for cand in CANDIDATES:
        provider = cand["provider"]
        model = cand["model"]
        print(f"--- {provider}/{model} ---")
        start = time.monotonic()

        try:
            if provider == "gemini":
                api_key = os.environ.get("GEMINI_API_KEY")
                if not api_key:
                    print("  SKIP (no GEMINI_API_KEY)")
                    results.append({"provider": provider, "model": model, "status": "skip", "reason": "no key"})
                    continue
                reply = _probe_gemini(api_key, model, image_b64)
            elif provider == "mistral":
                api_key = os.environ.get("MISTRAL_API_KEY")
                if not api_key:
                    print("  SKIP (no MISTRAL_API_KEY)")
                    results.append({"provider": provider, "model": model, "status": "skip", "reason": "no key"})
                    continue
                reply = _probe_mistral(api_key, model, image_b64)
            elif provider == "local":
                endpoint = cand.get("endpoint", "http://127.0.0.1:11434")
                reply = _probe_local(endpoint, model, image_b64)
            else:
                print("  SKIP (unknown provider)")
                continue

            elapsed = time.monotonic() - start
            print(f"  Reply ({elapsed:.1f}s): {reply[:200]}")
            certified = any(kw in reply.lower() for kw in ["red", "blue", "7", "seven", "digit"])
            status = "certified" if certified else "uncertain"
            results.append({"provider": provider, "model": model, "status": status, "reply": reply[:200], "latency": round(elapsed, 1)})
            print(f"  → {status.upper()}")
        except Exception as exc:
            elapsed = time.monotonic() - start
            print(f"  ERROR ({elapsed:.1f}s): {exc}")
            results.append({"provider": provider, "model": model, "status": "error", "error": str(exc)[:200]})

        print()

    print("=" * 60)
    print("Summary")
    print("=" * 60)
    certified = [r for r in results if r.get("status") == "certified"]
    uncertain = [r for r in results if r.get("status") == "uncertain"]
    errors = [r for r in results if r.get("status") == "error"]
    skipped = [r for r in results if r.get("status") == "skip"]

    print(f"  Certified: {len(certified)}")
    for r in certified:
        print(f"    ✅ {r['provider']}/{r['model']} ({r.get('latency', '?')}s)")
    print(f"  Uncertain: {len(uncertain)}")
    for r in uncertain:
        print(f"    ❓ {r['provider']}/{r['model']} — {r.get('reply', '')[:80]}")
    print(f"  Errors: {len(errors)}")
    for r in errors:
        print(f"    ❌ {r['provider']}/{r['model']} — {r.get('error', '')[:80]}")
    print(f"  Skipped: {len(skipped)}")
    for r in skipped:
        print(f"    ⏭  {r['provider']}/{r['model']} — {r.get('reason', '')}")

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
