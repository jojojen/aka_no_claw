# Fix Benchmarks

Last reviewed: 2026-06-24
Status: Current
Owner area: agent-maintenance

This directory contains deterministic parser/extractor repair benchmarks and
quality benchmarks for future repair workflows.

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

Commerce/parser fixtures must be useful for parser repair without becoming
realistic clones of live commerce sites.

Required for synthetic commerce fixtures:

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
SLSH  seller-profile / review snapshot style source
```

These codes are not brands. They only label fixture families. Real sources may
inspire the generic source type, but benchmark files must not contain real
merchant names or real source URLs.

Required for public-media quality fixtures:

- Use publicly available media with a recorded source URL, author, and license.
- Re-save local image copies without EXIF/GPS metadata.
- Avoid private personal data. Public venue artifacts are acceptable only when
  the benchmark objective is OCR/translation quality and not contact extraction.
- Keep source attribution in metadata files, not hidden in binary metadata.

## Current Benchmarks

| Benchmark | Purpose |
|---|---|
| [price_reference_sources](price_reference_sources/README.md) | Multi-source card/product price-reference parser benchmark. |
| [seller_snapshot_sources](seller_snapshot_sources/README.md) | Seller profile, review, and cooldown lifecycle benchmark. |
| [image_translation_policy](image_translation_policy/README.md) | Public-media image translation policy benchmark. |

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

The reference parser should pass. A `/fix` implementation should produce
a patch that reaches the same verifier result without hardcoding fixture file
names or complete fixture outputs.

## The `/fix` Command

The Telegram `/fix` command (`src/openclaw_adapter/fix_command.py`) runs the
repair loop against these benchmarks:

- `/fix` — list benchmarks discovered here (any directory holding `verify.py`
  plus `broken/` with a single `parser.py` or `classifier.py`).
- `/fix <benchmark_name>` — reproduce the failure, ask the repair LLM
  (OpenCode big-pickle, falling back to local Ollama with an explicit warning)
  for a replacement module, run the verifier, and iterate up to 4 attempts.
- On PASS the bot sends a unified diff with an apply button. Applying persists
  the candidate to the benchmark's `attempts/` directory; `broken/` is never
  modified, so the benchmark keeps reproducing the original failure.

v1 scope is benchmark parsers only — `/fix` does not touch production code.

### Known limitation: verifier output leaks expected values

Verifier failure lines include the expected values ("expected 'X', got None"),
so a degenerate patch could in principle hardcode per-fixture outputs.
Mitigations in v1: fixtures span multiple structural revisions (hardcoding one
selector or one output does not generalize), and a human reviews the diff
before apply. A hidden holdout fixture is a possible follow-up.
