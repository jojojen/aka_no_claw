# Test Record 2026-04-16

## Scope

This record covers the multi-source lookup update that extends card-price lookup beyond Yuyutei.

Primary goals for this round:

- keep the existing Yuyutei lookup working
- add reliable secondary reference sources that participate in real lookup results
- reduce false "no result" cases by searching Cardrush Pokemon and Magi in addition to Yuyutei
- keep the assistant / TCG boundary clean while expanding source coverage

Environment used:

- Windows PowerShell
- repo-local `.venv`
- local `.env` loaded for runtime settings

## Code Paths Verified

- `src/tcg_tracker/cardrush.py`
  - new Cardrush Pokemon lookup client
  - uses live search results from `product-list?keyword=...`
- `src/tcg_tracker/magi.py`
  - new Magi product lookup client
  - supports both Pokemon and Weiss Schwarz search result parsing
- `src/tcg_tracker/service.py`
  - now aggregates offers from multiple reference clients
  - deduplicates repeated listings across multiple search terms
- `src/tcg_tracker/matching.py`
  - set-code matching now accepts `set_code` fallback, not only `version_code`
  - graded Magi listings receive a small penalty so raw copies stay preferred
- `src/tcg_tracker/hot_cards.py`
  - Cardrush card numbers are stripped cleanly
  - Magi parsing now understands inline Pokemon card numbers like `349/190`
- `src/openclaw_adapter/formatters.py`
  - lookup output is now source-neutral
  - output shows best ask / market / bid and a source summary

## Automated Tests

Command:

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest
```

Result:

- `34 passed`

Tests added or updated in this round:

- `tests/test_marketplace_clients.py`
  - verifies Cardrush Pokemon parsing and matching
  - verifies Magi WS parsing and matching
  - verifies Magi Pokemon parsing for inline card numbers
- `tests/test_tcg_service.py`
  - verifies the service aggregates multiple sources into a single lookup result

## Live Verification

### 1. Pokemon multi-source lookup for broad query

Command:

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m openclaw_adapter tcg.lookup-card --game pokemon --name "メガシビルドン" --rarity SAR
```

Observed result:

- result was no longer source-empty
- sources included `cardrush_pokemon`, `magi`, and `yuyutei`
- best ask came from Cardrush
- best market came from Magi
- best bid came from Yuyutei
- query stayed marked as ambiguous because multiple variants matched, which is correct for a name-only + rarity lookup

Observed values during verification:

- best ask: `¥480` from `cardrush_pokemon`
- best market: `¥729` from `magi`
- best bid: `¥300` from `yuyutei`
- source summary: `cardrush_pokemon x4, magi x1, yuyutei x2`

### 2. Pokemon precise lookup for Charizard example

Command:

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m openclaw_adapter tcg.lookup-card --game pokemon --name "リザードンex" --card-number "349/190" --rarity "SAR" --set-code "sv4a"
```

Observed result:

- precise match succeeded
- fair value was computed
- result included offers from `cardrush_pokemon`, `magi`, and `yuyutei`

Observed values during verification:

- fair value: `¥37,800`
- best ask: `¥23,800` from `cardrush_pokemon`
- best market: `¥9,980` from `magi`
- best bid: `¥42,000` from `yuyutei`
- source summary: `cardrush_pokemon x9, magi x3, yuyutei x2`

### 3. WS Hatsune Miku sample requested by the user

Command:

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m openclaw_adapter tcg.lookup-card --game ws --name "ワンダーランドのセカイ 初音ミク" --card-number "PJS/S91-T51"
```

Observed result:

- precise WS lookup succeeded
- current live result came from Yuyutei
- no regression from the new multi-source aggregation

Observed values during verification:

- fair value: `¥80`
- best ask: `¥80` from `yuyutei`
- source summary: `yuyutei x1`

### 4. WS multi-source verification

Command:

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m openclaw_adapter tcg.lookup-card --game ws --name "“夏の思い出”蒼(サイン入り)" --card-number "SMP/W60-051SP" --rarity "SP" --set-code "smp"
```

Observed result:

- precise WS lookup succeeded
- result included both `yuyutei` and `magi`
- this confirmed Magi is active as a secondary WS reference source in the live pipeline

Observed values during verification:

- fair value: `¥17,800`
- best ask: `¥17,800` from `yuyutei`
- best market: `¥22,800` from `magi`
- best bid: `¥10,000` from `yuyutei`
- source summary: `magi x1, yuyutei x2`

## Notes

- On Windows PowerShell, Japanese CLI output is safest with:

```powershell
$env:PYTHONIOENCODING='utf-8'
```

- The new multi-source lookup is intentionally opportunistic:
  - if secondary sources have usable matches, they are included
  - if a secondary source has no usable match at that moment, the result can still legitimately be Yuyutei-only

- The broad `メガシビルドン + SAR` query is a good regression case because it demonstrates the actual goal of this round:
  - do not fail empty
  - surface usable secondary-source offers
  - keep ambiguity handling honest when multiple variants still match
