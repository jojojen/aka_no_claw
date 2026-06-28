# Price Reference Sources Benchmark

Last reviewed: 2026-06-24
Status: Current
Owner area: agent-maintenance

This benchmark tests repair of product/card price-reference parsers across
multiple synthetic source families.

All source names are fictional fixture codes:

```text
KNSR  secondary-market style listing source
AUCL  auction-style listing source
PUBR  publisher-release style source
TCGW  card-shop catalog style source
```

The fixture names are deliberately not real stores. Do not replace them with
real merchant names inside this benchmark.

## Goal

Repair a broken parser so it extracts one normalized price reference record from
each synthetic HTML fixture.

Expected output schema:

```json
{
  "source_code": "KNSR",
  "source_type": "secondary_market",
  "item_id": "KNSR-2026-0001",
  "title": "青空レリック プロモカード 042/999",
  "price_jpy": 12800,
  "price_kind": "ask",
  "availability": "available",
  "seller_or_store": "knsr-lab",
  "condition": "near_mint"
}
```

## Fixture Coverage

| Fixture | Source family | Failure mode represented |
|---|---|---|
| [fixtures/knsr_listing_v1.html](fixtures/knsr_listing_v1.html) | Secondary-market listing | Direct semantic attributes. |
| [fixtures/knsr_listing_v2.html](fixtures/knsr_listing_v2.html) | Secondary-market listing | Data moved into a JSON island with stale selector decoys. |
| [fixtures/knsr_listing_v3.html](fixtures/knsr_listing_v3.html) | Secondary-market result grid | Product tile attributes are shuffled; price appears in both an accessible label and nested spans, with stale tile decoys nearby. |
| [fixtures/aucl_auction_v1.html](fixtures/aucl_auction_v1.html) | Auction listing | Current bid, auction status, and seller are in mixed visible text and metadata. |
| [fixtures/pubr_release_v1.html](fixtures/pubr_release_v1.html) | Publisher product/release | MSRP and release status appear in a definition list. |
| [fixtures/tcgw_catalog_v1.html](fixtures/tcgw_catalog_v1.html) | Card-shop catalog | Price and stock data appear in a table row. |

The fixtures intentionally include real-world parser annoyances:

- noisy layout wrappers and unrelated sidebar modules,
- stale selector decoys,
- attribute-order changes that break regex-only product-tile parsers,
- data split across attributes, visible text, metadata, tables, and JSON islands,
- mixed Japanese and English labels,
- sponsored/decoy rows,
- lazy image placeholders and irrelevant hydration data.

They still must remain visibly synthetic and must not use real merchant names,
real URLs, copied CSS, copied wording, or brand-adjacent visual identity.

This is meant to become the seed for future `/fix` work: when a new price
source parser fails during development, reduce the failure into another
synthetic fixture here.

## Files

```text
fixtures/        HTML snapshots
expected/        expected structured output for each fixture
broken/parser.py intentionally broken parser
reference/parser.py stdlib-only reference implementation
verify.py        deterministic verifier
```

The captured parser failure history is in [FAILURE_TRACE.md](FAILURE_TRACE.md).

## Running

Broken parser, expected to fail:

```bash
python3 docs/fix_benchmarks/price_reference_sources/verify.py \
  --parser docs/fix_benchmarks/price_reference_sources/broken/parser.py
```

Reference parser, expected to pass:

```bash
python3 docs/fix_benchmarks/price_reference_sources/verify.py \
  --parser docs/fix_benchmarks/price_reference_sources/reference/parser.py
```

## Repair Rules

A repaired parser should:

- parse all fixtures without network access,
- use generalized extraction rules rather than complete fixture-output
  hardcoding,
- normalize price to an integer JPY value,
- normalize source type, price kind, availability, and condition to the expected
  schema,
- ignore stale selector decoys,
- return only the expected schema keys.

## Future Holdout Guidance

The fixtures under `docs/` are public calibration fixtures. Future evaluation
can add private holdout fixtures outside `docs/` using the same schema and
verifier contract.
