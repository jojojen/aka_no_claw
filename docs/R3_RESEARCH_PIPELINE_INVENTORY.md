# R3 — Research Pipeline Responsibility Inventory

Last reviewed: 2026-07-13
Status: Current — R3.0 characterization for issue #76.
Owner area: research

`research_command.py` is the public compatibility facade for `/research`.  Its
current implementation combines request parsing, staged scheduling, Mercari
item extraction, market evidence, seller evidence, report rendering and title
similarity.  These concerns must be extracted without changing source weighting,
provenance, partial-result, progress, or cancellation behaviour.

## Target modules

| Module | Responsibility | Current symbols |
|---|---|---|
| `research/models.py` | Typed stage/result/report envelopes and budgets | `ResearchTarget`, `ItemData`, `EntityProfile`, `PriceEvidence`, `ShopReference`, `SellerReputationSnapshot`, `ResearchSectionResult`, `ResearchJobContext`, `ResearchReport`, `ResearchBudget` |
| `research/input.py` | Request and Mercari URL normalization | `parse_research_target`, `normalize_mercari_item_url`, `normalize_mercari_shops_url` |
| `research/item.py` | Item-page fetch and extraction | `MercariItemAdapter`, `build_research_item_fetch_html` and HTML helpers |
| `research/stages.py` | Explicit stage runners and scheduler | `ResearchCommandService` stage methods |
| `research/market.py` | Comparable, price, liquidity and title evidence | market/search helpers and price/liquidity section builders |
| `research/reporting.py` | Report synthesis and renderers | `build_research_report`, `format_research_*` |
| `research/service.py` | Construction/wiring behind the public facade | `build_research_handler` |

## Invariants

1. Every final report retains URLs, warnings and partial-stage status.
2. A shared `ResearchBudget` remains thread-safe across concurrent stages.
3. Cancellation checks remain before every stage and within budgeted searches.
4. Fetch/scrape failures degrade to visible stage evidence; they never fabricate
   an equivalent source or silently return a complete report.
5. The public `openclaw_adapter.research_command` import surface remains stable.

## Characterization cases

- URL and product-name inputs;
- no comparable offers, unavailable seller/vision source, 429 and slow stage;
- cancellation, progress messages, partial evidence and provenance;
- complete/compact/detail report rendering.

Completed: `research/models.py` now owns the typed contracts, shared budget and
thread-safe cancellation/progress envelope; `research_command.py` re-exports
the established surface for compatibility.  Tests: `tests/test_research_command.py`
and `tests/test_research_command_boundaries.py`.

The stage envelope records a schema version, payload, provenance URLs, terminal
failure class, elapsed time, host-request count and cache-freshness marker.
Legacy stage constructors remain compatible; `ResearchJobContext` fills stable
defaults when a stage returns only the established result fields.

Completed: `research/input.py` owns text/URL normalization and canonical Mercari
target construction, while the facade preserves its public helper imports.

Completed: `research_command.py` is now a stable thin compatibility facade;
`research/service.py` owns the existing stage orchestration while consumers keep
the same module-level seams.

Completed: `research/scheduler.py` owns the sequential/parallel stage ordering,
overall marketplace deadline, progress heartbeat and cancellation lifecycle;
the service injects its configured limits and report builder explicitly.

Completed: `research/stages.py` owns normalized-input and item-fetch stage
results, including visible partial/unavailable evidence contracts.

Completed: `research/stages.py` also owns entity-recognition persistence and
vision-condition stage outcomes.  Both retain their previous explicit
unavailable/partial evidence instead of promoting a failed optional backend to
success.

Completed: the seller/reputation stage now lives in `research/stages.py`,
including Mercari Shops exclusion, unavailable adapters, pending snapshots,
asynchronous follow-up and visible partial failure evidence.

Completed: the appreciation/context stage is an explicit `research/stages.py`
operation, with knowledge lookup, optional bounded search enrichment and source
provenance passed into the existing report builder unchanged.

Completed: `research/market.py` owns the active-listing price-cap policy used by
the price-evidence stage; a sold average now expands the cap for name-mode
research without changing the existing listed-price precedence.
It also owns the liquidity stage's hand-off from the shared comparable evidence
set, so price and demand remain provenance-aligned.

The #76 boundary work is complete: callers use the thin facade, and the typed
contracts, input, scheduler, explicit stages, report synthesis and market
policy each have dedicated collaborators.  Additional movement of low-level
Mercari parsing or report-rendering helpers is ordinary follow-on maintenance,
not a second orchestration pipeline.
