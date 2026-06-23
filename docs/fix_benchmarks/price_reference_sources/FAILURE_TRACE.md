# Price Reference Sources Failure Trace

Last reviewed: 2026-06-24
Status: Current
Owner area: agent-maintenance

This file records the reproducible parser repair state used by this benchmark.

All source names are synthetic fixture codes. The benchmark intentionally avoids
real merchant names, real merchant URLs, copied page text, copied CSS, and
brand-adjacent visual identity.

## Attempt 01: naive DOM parser

Parser:

```text
docs/fix_benchmarks/price_reference_sources/attempts/attempt_01_naive_dom.py
```

Command:

```bash
python3 docs/fix_benchmarks/price_reference_sources/verify.py \
  --parser docs/fix_benchmarks/price_reference_sources/attempts/attempt_01_naive_dom.py
```

Observed result:

```text
FAIL aucl_auction_v1
PASS knsr_listing_v1
FAIL knsr_listing_v2
FAIL knsr_listing_v3
FAIL pubr_release_v1
FAIL tcgw_catalog_v1

Pass rate: 1/6 = 17%
```

Reason:

- It only understands direct `data-testid` fields.
- It cannot read JSON islands.
- It cannot extract primary product tiles when attributes are shuffled.
- It cannot read publisher-release definition lists.
- It cannot read auction current-bid layouts.
- It cannot choose the primary table row in catalog pages.

## Attempt 02: DOM + JSON island parser

Parser:

```text
docs/fix_benchmarks/price_reference_sources/broken/parser.py
```

Command:

```bash
python3 docs/fix_benchmarks/price_reference_sources/verify.py \
  --parser docs/fix_benchmarks/price_reference_sources/broken/parser.py
```

Observed result:

```text
FAIL aucl_auction_v1
PASS knsr_listing_v1
PASS knsr_listing_v2
FAIL knsr_listing_v3
FAIL pubr_release_v1
FAIL tcgw_catalog_v1

Pass rate: 2/6 = 33%
```

Remaining failures:

- `aucl_auction_v1`: current bid, auction status, seller, and condition are
  stored in auction-specific text/metadata.
- `knsr_listing_v3`: the primary product tile uses shuffled attributes,
  accessible-label price text, nested visible price spans, and nearby stale tile
  decoys.
- `pubr_release_v1`: MSRP, release status, publisher channel, and condition are
  stored in a definition list.
- `tcgw_catalog_v1`: the relevant product is a primary table row among decoys.

This is the intended `/fix` starting point.

## Reference parser

Parser:

```text
docs/fix_benchmarks/price_reference_sources/reference/parser.py
```

Command:

```bash
python3 docs/fix_benchmarks/price_reference_sources/verify.py \
  --parser docs/fix_benchmarks/price_reference_sources/reference/parser.py
```

Observed result:

```text
PASS aucl_auction_v1
PASS knsr_listing_v1
PASS knsr_listing_v2
PASS knsr_listing_v3
PASS pubr_release_v1
PASS tcgw_catalog_v1

Pass rate: 6/6 = 100%
Verifier PASSED.
```

## Benchmark Task

The repair task is:

```text
Starting from broken/parser.py, add generalized extraction support for:
1. auction current-bid pages,
2. shuffled-attribute product tiles with accessible-label and nested-span price
   fallbacks,
3. publisher-release definition-list pages,
4. card-shop catalog tables with decoy rows.
```

The repair must keep KNSR fixtures passing and must not hardcode complete
fixture outputs.
