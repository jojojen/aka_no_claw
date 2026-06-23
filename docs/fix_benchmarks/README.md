# Fix Benchmarks

Last reviewed: 2026-06-24
Status: Current
Owner area: agent-maintenance

This directory contains deterministic parser/extractor repair benchmarks for
the future `/fix` workflow.

Each benchmark should provide:

```text
broken implementation
+ local fixtures
+ expected structured outputs
+ deterministic verifier
```

The intended repair loop is:

```text
reproduce failure
-> propose patch
-> run verifier
-> allow apply only if verifier passes
```

## Safety Rules

Fixtures must be useful for parser repair without becoming realistic clones of
live commerce sites.

Required:

- Use fictional source names, sellers, item IDs, URLs, CSS classes, and copy.
- Do not mention real merchant names inside benchmark files.
- Do not copy real logos, exact CSS, exact page text, account flows, checkout
  flows, or seller contact flows from live services.
- Make every page visibly labeled as a synthetic benchmark fixture.
- Preserve generic price-reference semantics such as item title, source type,
  price, stock/status, seller/store, condition, and shipping or release fields.
- Keep fixtures fully local and deterministic.

Recommended fictional source codes:

```text
KNSR  secondary-market style listing source
AUCL  auction-style listing source
PUBR  publisher-release style source
TCGW  card-shop catalog style source
```

These codes are not brands. They only label fixture families. Real sources may
inspire the generic source type, but benchmark files must not contain real
merchant names or real source URLs.

## Current Benchmarks

| Benchmark | Purpose |
|---|---|
| [price_reference_sources](price_reference_sources/README.md) | Multi-source card/product price-reference parser benchmark. |

## Running A Benchmark

Use Python 3 and no third-party dependencies:

```bash
python3 docs/fix_benchmarks/price_reference_sources/verify.py \
  --parser docs/fix_benchmarks/price_reference_sources/broken/parser.py
```

The broken parser should fail.

Then run the reference parser:

```bash
python3 docs/fix_benchmarks/price_reference_sources/verify.py \
  --parser docs/fix_benchmarks/price_reference_sources/reference/parser.py
```

The reference parser should pass. A future `/fix` implementation should produce
a patch that reaches the same verifier result without hardcoding fixture file
names or complete fixture outputs.
