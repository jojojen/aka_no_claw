# Seller Snapshot Lifecycle Benchmark

Last reviewed: 2026-06-24
Status: Current
Owner area: agent-maintenance

This benchmark tests the non-parser branch of seller snapshot repair: deciding
what the capture worker should do when a seller page cannot be fetched.

The correct answer for rate limiting is not to guess profile data and not to
keep retrying immediately. It is to return a cooldown decision that the outer
queue can schedule after the wait window.

## Goal

Repair a broken lifecycle classifier so it returns one normalized action record
for each synthetic capture outcome.

Expected output schema:

```json
{
  "case_id": "rate-limit-429",
  "action": "cooldown_wait",
  "retry_after_seconds": 600,
  "should_parse": false,
  "should_requeue": true,
  "reason": "rate_limited"
}
```

## Fixture Coverage

| Fixture | Failure mode represented |
|---|---|
| [fixtures/rate_limit_429.json](fixtures/rate_limit_429.json) | HTTP 429 with Retry-After. Correct action is wait for cooldown and requeue. |
| [fixtures/interstitial_block.json](fixtures/interstitial_block.json) | Synthetic bot-check/interstitial shell. Correct action is wait for cooldown and requeue. |
| [fixtures/normal_profile_ready.json](fixtures/normal_profile_ready.json) | Normal profile HTML is ready to parse immediately. |

All fixtures are synthetic and contain no real merchant names, real seller
identifiers, real URLs, real page text, or copied HTML.

## Running

Broken classifier, expected to fail:

```bash
python3 docs/fix_benchmarks/seller_snapshot_sources/lifecycle/verify.py \
  --classifier docs/fix_benchmarks/seller_snapshot_sources/lifecycle/broken/classifier.py
```

Reference classifier, expected to pass:

```bash
python3 docs/fix_benchmarks/seller_snapshot_sources/lifecycle/verify.py \
  --classifier docs/fix_benchmarks/seller_snapshot_sources/lifecycle/reference/classifier.py
```
