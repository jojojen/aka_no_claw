"""Domain Registry: domain → source-type / trust / display metadata (issue #11).

Sits on top of the #9 source registry. A source row already stores its host
(``domain``); this module answers the *domain-level* questions #9 could not:
what kind of source is this host, how far should we trust it by default, and
what compact human label a citation should show.

The priors are static and in-code (Deliverable 3 says static defaults are
enough): a coarse prior for downstream RAG / opportunity scoring, **not** a
reputation model (see the issue's Non-goals). Domain ids are derived
deterministically from the canonical host, and host variants / aliases all
resolve to the same record — so a source row references its domain record by id
without storing a redundant column: ``get_domain(rec.domain)`` resolves it at
read time (backward compatible with every existing source row).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .url_canonicalize import source_domain

# ── Deliverable 2: closed source-type vocabulary ─────────────────────────────
SOURCE_TYPES: tuple[str, ...] = (
    "official",
    "marketplace",
    "sns",
    "search",
    "aggregator",
    "news",
    "forum",
    "blog",
    "other",
)
_SOURCE_TYPE_SET = frozenset(SOURCE_TYPES)
DEFAULT_SOURCE_TYPE = "other"

# Human label per source type for compact citations, e.g. "(Marketplace)".
_SOURCE_TYPE_LABEL: dict[str, str] = {
    "official": "Official",
    "marketplace": "Marketplace",
    "sns": "SNS",
    "search": "Search",
    "aggregator": "Aggregator",
    "news": "News",
    "forum": "Forum",
    "blog": "Blog",
    "other": "Other",
}

# ── Deliverable 3: default trust prior per source type (coarse, static) ───────
_TRUST_BY_SOURCE_TYPE: dict[str, float] = {
    "official": 1.00,
    "marketplace": 0.92,
    "news": 0.80,
    "aggregator": 0.55,
    "sns": 0.55,
    "forum": 0.45,
    "blog": 0.45,
    "search": 0.28,
    "other": 0.30,
}


def normalize_source_type(value: str | None) -> str:
    """Snap a source-type token to the closed vocabulary; unknown → ``other``."""
    token = (value or "").strip().lower()
    return token if token in _SOURCE_TYPE_SET else DEFAULT_SOURCE_TYPE


def clamp_trust(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def trust_for_source_type(source_type: str) -> float:
    """Default trust prior for a source type (used when a domain carries no
    explicit score)."""
    return _TRUST_BY_SOURCE_TYPE[normalize_source_type(source_type)]


def source_type_label(source_type: str) -> str:
    return _SOURCE_TYPE_LABEL[normalize_source_type(source_type)]


def build_domain_id(domain: str) -> str:
    """Stable id derived from a canonical host, e.g. ``suruga-ya.jp`` →
    ``dom_surugayajp``. Deterministic so a citation id stays valid forever."""
    slug = re.sub(r"[^a-z0-9]+", "", (domain or "").lower())
    return f"dom_{slug}"


# ── Deliverable 1: Domain Registry model ─────────────────────────────────────
@dataclass(frozen=True, slots=True)
class DomainRecord:
    domain_id: str
    domain: str
    display_name: str
    source_type: str
    trust_score: float
    aliases: tuple[str, ...] = ()
    notes: str | None = None


def make_domain_record(
    *,
    domain: str,
    display_name: str,
    source_type: str,
    trust_score: float | None = None,
    aliases: tuple[str, ...] = (),
    notes: str | None = None,
) -> DomainRecord:
    """Build a normalized DomainRecord: closed-vocabulary source type, clamped
    trust, and trust derived from the source type when none is given."""
    stype = normalize_source_type(source_type)
    score = trust_for_source_type(stype) if trust_score is None else clamp_trust(trust_score)
    canonical = _normalize_host(domain)
    return DomainRecord(
        domain_id=build_domain_id(canonical),
        domain=canonical,
        display_name=display_name,
        source_type=stype,
        trust_score=score,
        aliases=tuple(_normalize_host(a) for a in aliases),
        notes=notes,
    )


def _normalize_host(value: str) -> str:
    """Reduce a host or URL to its registry key, e.g. ``https://www.X.com/p`` →
    ``x.com``. Reuses #9's ``source_domain`` for URLs; bare hosts are lowered and
    stripped of a leading ``www.`` and any port."""
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if "/" in raw or ":" in raw and "//" in raw:
        host = source_domain(raw)
        if host:
            return host
    host = raw.split("/", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host.split(":", 1)[0]


# ── Seed domains (issue #11 "Suggested Seed Domains") ─────────────────────────
# trust_score=None → derived from the source type's default prior.
_SEED_SPECS: tuple[dict, ...] = (
    # Official
    {"domain": "pokemon-card.com", "display_name": "Pokémon Card Official", "source_type": "official"},
    {"domain": "ws-tcg.com", "display_name": "Weiß Schwarz Official", "source_type": "official"},
    {"domain": "onepiece-cardgame.com", "display_name": "ONE PIECE Card Game Official", "source_type": "official"},
    {"domain": "yugioh-card.com", "display_name": "Yu-Gi-Oh! Card Official", "source_type": "official"},
    {"domain": "unionarena-tcg.com", "display_name": "UNION ARENA Official", "source_type": "official"},
    {"domain": "bandai.co.jp", "display_name": "Bandai", "source_type": "official"},
    # Marketplace / store
    {"domain": "suruga-ya.jp", "display_name": "Suruga-ya", "source_type": "marketplace", "trust_score": 0.95},
    {"domain": "jp.mercari.com", "display_name": "Mercari", "source_type": "marketplace",
     "aliases": ("mercari.com",)},
    {"domain": "yuyu-tei.jp", "display_name": "Yuyu-tei", "source_type": "marketplace"},
    {"domain": "cardrush-pokemon.jp", "display_name": "Cardrush", "source_type": "marketplace"},
    {"domain": "magi.camp", "display_name": "magi", "source_type": "marketplace"},
    {"domain": "rakuma.rakuten.co.jp", "display_name": "Rakuma", "source_type": "marketplace"},
    {"domain": "shopping.yahoo.co.jp", "display_name": "Yahoo! Shopping", "source_type": "marketplace"},
    # SNS / forum / search
    {"domain": "x.com", "display_name": "X", "source_type": "sns", "aliases": ("twitter.com",)},
    {"domain": "boards.4chan.org", "display_name": "4chan", "source_type": "forum"},
    {"domain": "search.yahoo.co.jp", "display_name": "Yahoo! Search", "source_type": "search"},
    {"domain": "rd.listing.yahoo.co.jp", "display_name": "Yahoo Redirect", "source_type": "search",
     "trust_score": 0.20, "notes": "opaque redirect plumbing"},
    {"domain": "google.com", "display_name": "Google", "source_type": "search"},
)


def _build_index() -> tuple[dict[str, DomainRecord], dict[str, DomainRecord]]:
    """Return (host→record, domain_id→record). Canonical host and every alias
    map to the same record."""
    by_host: dict[str, DomainRecord] = {}
    by_id: dict[str, DomainRecord] = {}
    for spec in _SEED_SPECS:
        rec = make_domain_record(**spec)
        by_id[rec.domain_id] = rec
        by_host[rec.domain] = rec
        for alias in rec.aliases:
            by_host[alias] = rec
    return by_host, by_id


_BY_HOST, _BY_ID = _build_index()


# ── Deliverable 6: scoring / rendering helper APIs ───────────────────────────
def get_domain(domain_or_id: str) -> DomainRecord | None:
    """Resolve a seeded domain record by domain id (``dom_*``), host string, or
    full URL. Returns None for an empty input or an unseeded host."""
    key = (domain_or_id or "").strip()
    if not key:
        return None
    if key.lower().startswith("dom_"):
        return _BY_ID.get(key.lower())
    return _BY_HOST.get(_normalize_host(key))


def get_source_type(domain_or_id: str) -> str:
    """Source type for a domain; unseeded hosts fall back to ``other``."""
    rec = get_domain(domain_or_id)
    return rec.source_type if rec is not None else DEFAULT_SOURCE_TYPE


def get_domain_trust(domain_or_id: str) -> float:
    """Default trust prior for a domain; unseeded hosts fall back to the
    ``other`` prior. Always clamped to [0.0, 1.0]."""
    rec = get_domain(domain_or_id)
    if rec is not None:
        return rec.trust_score
    return trust_for_source_type(DEFAULT_SOURCE_TYPE)


def domain_citation_label(domain_or_id: str) -> str:
    """Compact human label for a citation: ``Suruga-ya (Marketplace)`` for a
    seeded domain, else the bare host (e.g. ``example.com``) so unseeded sources
    still render cleanly (issue #11 D5)."""
    rec = get_domain(domain_or_id)
    if rec is not None:
        return f"{rec.display_name} ({source_type_label(rec.source_type)})"
    return _normalize_host(domain_or_id) or (domain_or_id or "").strip()
