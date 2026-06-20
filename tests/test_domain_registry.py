"""Domain Registry (issue #11): domain → source-type / trust / display label.

A coarse, static prior layered on the #9 source registry — host variants and
aliases resolve to one record; unseeded hosts degrade to the ``other`` prior."""

from __future__ import annotations

import pytest

from openclaw_adapter.domain_registry import (
    DEFAULT_SOURCE_TYPE,
    SOURCE_TYPES,
    build_domain_id,
    clamp_trust,
    domain_citation_label,
    get_domain,
    get_domain_trust,
    get_source_type,
    make_domain_record,
    normalize_source_type,
    source_type_label,
    trust_for_source_type,
)


# ── Deliverable 2: closed source-type vocabulary ─────────────────────────────
def test_normalize_source_type_snaps_unknown_to_other():
    assert normalize_source_type("OFFICIAL") == "official"
    assert normalize_source_type("  Marketplace ") == "marketplace"
    assert normalize_source_type("wat") == DEFAULT_SOURCE_TYPE
    assert normalize_source_type(None) == DEFAULT_SOURCE_TYPE
    assert normalize_source_type("") == DEFAULT_SOURCE_TYPE


def test_every_source_type_has_label_and_trust():
    for stype in SOURCE_TYPES:
        assert source_type_label(stype)  # non-empty
        assert 0.0 <= trust_for_source_type(stype) <= 1.0


# ── Deliverable 3: trust prior, clamp, derive-from-type ──────────────────────
def test_clamp_trust_bounds():
    assert clamp_trust(-1.0) == 0.0
    assert clamp_trust(2.0) == 1.0
    assert clamp_trust(0.42) == pytest.approx(0.42)


def test_trust_derived_from_type_when_unset():
    rec = make_domain_record(domain="foo.example", display_name="Foo", source_type="news")
    assert rec.trust_score == pytest.approx(trust_for_source_type("news"))


def test_explicit_trust_is_clamped():
    rec = make_domain_record(
        domain="foo.example", display_name="Foo", source_type="blog", trust_score=5.0)
    assert rec.trust_score == 1.0


def test_unknown_source_type_falls_back_to_other_prior():
    rec = make_domain_record(domain="foo.example", display_name="Foo", source_type="bogus")
    assert rec.source_type == DEFAULT_SOURCE_TYPE
    assert rec.trust_score == pytest.approx(trust_for_source_type(DEFAULT_SOURCE_TYPE))


# ── domain_id derivation ─────────────────────────────────────────────────────
def test_build_domain_id_is_deterministic_and_slugged():
    assert build_domain_id("suruga-ya.jp") == "dom_surugayajp"
    assert build_domain_id("X.COM") == "dom_xcom"
    assert build_domain_id("suruga-ya.jp") == build_domain_id("suruga-ya.jp")


# ── Deliverable 6: lookups by host / id / url ────────────────────────────────
def test_get_domain_by_host():
    rec = get_domain("suruga-ya.jp")
    assert rec is not None
    assert rec.display_name == "Suruga-ya"
    assert rec.source_type == "marketplace"


def test_get_domain_by_host_variant_and_url():
    # leading www. + a full URL both reduce to the same record
    assert get_domain("https://www.suruga-ya.jp/item/1") is get_domain("suruga-ya.jp")


def test_get_domain_by_id():
    rec = get_domain("suruga-ya.jp")
    assert get_domain(rec.domain_id) is rec


def test_alias_resolves_to_same_record():
    assert get_domain("twitter.com") is get_domain("x.com")
    assert get_domain("mercari.com") is get_domain("jp.mercari.com")


def test_get_domain_unseeded_and_empty():
    assert get_domain("nope.invalid") is None
    assert get_domain("") is None
    assert get_domain(None) is None


def test_get_source_type_and_trust_fallback():
    assert get_source_type("suruga-ya.jp") == "marketplace"
    assert get_source_type("nope.invalid") == DEFAULT_SOURCE_TYPE
    assert get_domain_trust("suruga-ya.jp") == pytest.approx(0.95)  # explicit seed
    assert get_domain_trust("nope.invalid") == pytest.approx(
        trust_for_source_type(DEFAULT_SOURCE_TYPE))


# ── Deliverable 5: compact citation label ────────────────────────────────────
def test_citation_label_seeded_vs_unseeded():
    assert domain_citation_label("suruga-ya.jp") == "Suruga-ya (Marketplace)"
    assert domain_citation_label("https://www.x.com/foo") == "X (SNS)"
    # unseeded → bare normalized host, still readable
    assert domain_citation_label("https://www.example.com/p") == "example.com"
    assert domain_citation_label("") == ""
