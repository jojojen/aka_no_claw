# Seller Snapshot Sources Benchmark

Last reviewed: 2026-06-24
Status: Current
Owner area: agent-maintenance

This benchmark tests repair of seller-profile and seller-review snapshot
parsers using synthetic fixtures. It also contains a lifecycle sub-benchmark for
rate-limit and bot-interstitial capture outcomes.

All source names, profile names, IDs, and URLs are fictional. Do not replace
them with real marketplace names, real sellers, real profile URLs, copied CSS,
or copied page text.

## Goal

Repair a broken parser so it extracts one normalized seller snapshot record from
each synthetic HTML fixture.

Expected output schema:

```json
{
  "source_code": "SLSH",
  "profile_id": "SLSH-SELLER-0001",
  "display_name": "星野サンプル堂",
  "total_reviews": 152,
  "listing_count": 108,
  "followers_count": 3,
  "following_count": 38,
  "verified_badge": true,
  "seller_positive": 2,
  "seller_negative": 1,
  "buyer_positive": 1,
  "buyer_negative": 0
}
```

## Fixture Coverage

| Fixture | Failure mode represented |
|---|---|
| [fixtures/slsh_profile_basic_v1.html](fixtures/slsh_profile_basic_v1.html) | Direct semantic attributes on a synthetic seller profile. |
| [fixtures/slsh_profile_badge_noise_v1.html](fixtures/slsh_profile_badge_noise_v1.html) | The real review total is a standalone profile metric, while nearby badge and response-time text contain misleading numbers. |
| [fixtures/slsh_review_role_tabs_v1.html](fixtures/slsh_review_role_tabs_v1.html) | Seller-role and buyer-role reviews are split across tabs, so positive counts must not be collapsed into a single undifferentiated bucket. |

The fixtures intentionally include:

- synthetic seller chrome and noisy profile modules,
- standalone numeric profile metrics,
- badge and response-time number decoys,
- role-separated seller and buyer review tabs,
- mixed Japanese labels and compact numeric text.

They must remain visibly synthetic and must not use real merchant names, real
merchant URLs, real user names, copied CSS, copied wording, or brand-adjacent
visual identity.

## Files

```text
fixtures/        HTML snapshots
expected/        expected structured output for each fixture
broken/parser.py intentionally broken parser
reference/parser.py stdlib-only reference implementation
verify.py        deterministic verifier
```

The captured parser failure history is in [FAILURE_TRACE.md](FAILURE_TRACE.md).
The capture lifecycle trap cases are in [lifecycle/README.md](lifecycle/README.md).

## Running

Broken parser, expected to fail:

```bash
python3 docs/fix_benchmarks/seller_snapshot_sources/verify.py \
  --parser docs/fix_benchmarks/seller_snapshot_sources/broken/parser.py
```

Reference parser, expected to pass:

```bash
python3 docs/fix_benchmarks/seller_snapshot_sources/verify.py \
  --parser docs/fix_benchmarks/seller_snapshot_sources/reference/parser.py
```

Lifecycle classifier, expected to pass:

```bash
python3 docs/fix_benchmarks/seller_snapshot_sources/lifecycle/verify.py \
  --classifier docs/fix_benchmarks/seller_snapshot_sources/lifecycle/reference/classifier.py
```

## Repair Rules

A repaired parser should:

- parse all fixtures without network access,
- use generalized extraction rules rather than complete fixture-output
  hardcoding,
- normalize all counts to integers,
- keep seller-role and buyer-role review counts separate,
- avoid treating badge labels, response-time text, or other profile chrome as
  review totals,
- return only the expected schema keys.

The lifecycle classifier should return `cooldown_wait` for rate limits and
bot-interstitial captures. It must not parse empty/interstitial pages and must
not retry immediately while the source is cooling down.
