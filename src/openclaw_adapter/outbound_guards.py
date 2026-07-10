"""Outbound content guards for aka_no_claw.

Contract: fail ≠ push; raw dump ≠ content; guard at composition so every
surface (Telegram push, command replies, bridge) inherits.
"""
from __future__ import annotations

import json
import re

_PART_HEADER_RE = re.compile(r"^---\s+\S+\s+---\s*$", re.MULTILINE)

_META_PHRASES = (
    "as an ai",
    "i cannot access",
    "i would fetch",
    "it seems like the text got cut off",
    "text got cut off",
    "here's what i can provide",
    "this html page appears",
    "this page appears to",
)

_OPS_TOKENS = (
    "fetch_failed",
    "fetch failed",
    "http 403",
    "http 404",
    "http 429",
    "no_items_retrieved",
    "no content to evaluate",
    "traceback (most recent call last)",
)

_XML_PREFIXES = ("<?xml", "<rss", "<feed", "<html")


def _strip_part_headers(text: str) -> str:
    return _PART_HEADER_RE.sub("", text).strip()


def looks_like_raw_dump(text: str) -> str | None:
    body = _strip_part_headers(text)
    if not body:
        return None
    for prefix in _XML_PREFIXES:
        if body.lower().startswith(prefix):
            return f"XML/HTML payload detected (starts with {prefix!r})"
    stripped = body.lstrip()
    if stripped and stripped[0] in ("{", "[") and len(body) > 400:
        try:
            json.loads(body)
            return "raw JSON payload (parseable, >400 chars)"
        except json.JSONDecodeError:
            lines = stripped.splitlines()
            if len(lines) >= 3:
                json_line_count = sum(
                    1 for line in lines[:20]
                    if line.strip() and (
                        line.strip().endswith(("{", "[", ",", "},", "],", "}", "]"))
                        or re.match(r'^\s*"[^"]+"\s*:', line)
                    )
                )
                if json_line_count >= 3:
                    return "apparent JSON payload (structured, >400 chars)"
            else:
                # Single-line compact JSON: count "key": patterns
                key_matches = len(re.findall(r'"[^"]+"\s*:', stripped))
                if key_matches >= 4:
                    return "apparent JSON payload (compact single-line, >400 chars)"
    return None


def looks_like_meta_scaffolding(text: str) -> str | None:
    head = text[:1200].lower()
    tail = text[-800:].lower() if len(text) > 1200 else ""
    combined = head + tail
    for phrase in _META_PHRASES:
        if phrase in combined:
            return f"LLM meta-scaffolding phrase: {phrase!r}"
    return None


def proactive_push_failure(text: str) -> str | None:
    lower = text.lower()
    for token in _OPS_TOKENS:
        if token in lower:
            return f"ops failure token: {token!r}"
    return looks_like_raw_dump(text)


def guard_outbound(text: str, *, proactive: bool) -> str | None:
    reason = looks_like_meta_scaffolding(text)
    if reason:
        return reason
    if proactive:
        return proactive_push_failure(text)
    return looks_like_raw_dump(text)
