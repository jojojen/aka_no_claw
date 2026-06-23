# Seller Snapshot Sources Failure Trace

Last reviewed: 2026-06-24
Status: Current
Owner area: agent-maintenance

This file records the reproducible parser repair state used by this benchmark.

All source names are synthetic fixture codes. The benchmark intentionally avoids
real merchant names, real merchant URLs, real user names, copied page text,
copied CSS, and brand-adjacent visual identity.

## Source Of The Failure Shape

Repository history shows seller snapshot capture has been repaired repeatedly
around the same classes of behavior:

- slow snapshot jobs must become pending follow-ups rather than discarded work,
- stale jobs must be reclaimed instead of blocking the queue,
- cached proofs must be reused without unnecessary live review checks,
- profile parsers must not treat badge labels or response-time text as review
  totals,
- review entries must preserve seller-role and buyer-role separation.
- rate-limited captures must wait for cooldown instead of retrying immediately
  or parsing an interstitial shell.

The benchmark turns the parser-facing subset of those failures into deterministic
offline fixtures. The rate-limit behavior is covered by the lifecycle
sub-benchmark.

## Attempt 01: direct profile parser

Parser:

```text
docs/fix_benchmarks/seller_snapshot_sources/broken/parser.py
```

Command:

```bash
python3 docs/fix_benchmarks/seller_snapshot_sources/verify.py \
  --parser docs/fix_benchmarks/seller_snapshot_sources/broken/parser.py
```

Observed result:

```text
FAIL slsh_profile_badge_noise_v1
PASS slsh_profile_basic_v1
FAIL slsh_review_role_tabs_v1

Pass rate: 1/3 = 33%
```

Remaining failures:

- `slsh_profile_badge_noise_v1`: the parser reads the response-time decoy as
  the profile's total review count and misses the surrounding profile metrics.
- `slsh_review_role_tabs_v1`: the parser collapses or drops role-separated
  review tab counts.

This is the intended `/fix` starting point.

## Reference Parser

Parser:

```text
docs/fix_benchmarks/seller_snapshot_sources/reference/parser.py
```

Command:

```bash
python3 docs/fix_benchmarks/seller_snapshot_sources/verify.py \
  --parser docs/fix_benchmarks/seller_snapshot_sources/reference/parser.py
```

Observed result:

```text
PASS slsh_profile_badge_noise_v1
PASS slsh_profile_basic_v1
PASS slsh_review_role_tabs_v1

Pass rate: 3/3 = 100%
Verifier PASSED.
```

## Benchmark Task

The repair task is:

```text
Starting from broken/parser.py, add generalized extraction support for:
1. standalone profile metrics surrounded by badge and response-time decoys,
2. role-separated seller and buyer review tabs,
3. preserving direct semantic profile fixtures that already pass.
```

The repair must not hardcode complete fixture outputs.

## Lifecycle Trap: cooldown required

Classifier:

```text
docs/fix_benchmarks/seller_snapshot_sources/lifecycle/broken/classifier.py
```

Command:

```bash
python3 docs/fix_benchmarks/seller_snapshot_sources/lifecycle/verify.py \
  --classifier docs/fix_benchmarks/seller_snapshot_sources/lifecycle/broken/classifier.py
```

Observed result:

```text
FAIL interstitial_block
PASS normal_profile_ready
FAIL rate_limit_429

Pass rate: 1/3 = 33%
```

Correct repair:

```text
When HTTP 429 or a synthetic bot/interstitial shell appears, return:
action=cooldown_wait
should_parse=false
should_requeue=true
retry_after_seconds=Retry-After or default cooldown
```
