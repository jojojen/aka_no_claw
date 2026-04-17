# Test Record 2026-04-17

## Scope

This pass verified the liquidity-board refactor that removed listing count from the primary score.

Main changes under test:

- switch liquidity ranking from listing depth to buy-side support
- use 遊々亭 bid / ask relationship as the primary liquidity proxy
- add a modest boost when the store explicitly shows the buy quote was raised
- keep SNS only as a secondary attention signal
- update dashboard and Telegram output to show `bid`, `ask`, `bid/ask`, and `buy_support_score`
- update docs so the implemented logic and the displayed logic match

## Automated Tests

### Full suite

Command:

```powershell
C:\AI_Related\codex_work_space\.venv\Scripts\python.exe -m pytest
```

Result:

```text
40 passed
```

### Focused regression pass

Command:

```powershell
C:\AI_Related\codex_work_space\.venv\Scripts\python.exe -m pytest `
  C:\AI_Related\codex_work_space\tests\test_hot_cards.py `
  C:\AI_Related\codex_work_space\tests\test_dashboard.py `
  C:\AI_Related\codex_work_space\tests\test_telegram_bot.py
```

Result:

```text
17 passed
```

Coverage highlights:

- `tests/test_hot_cards.py`
  - duplicate variants are still merged
  - stronger buy-side support now beats better source-page rank
  - raw copies still outrank graded copies when buy support is the same
  - SNS remains secondary to liquidity
  - explicit `priceup` store-side buy signals give only a modest boost
- `tests/test_dashboard.py`
  - dashboard payload now includes `best_bid_jpy`, `best_ask_jpy`, `bid_ask_ratio`, `buy_support_score`, and store-side boost fields
- `tests/test_telegram_bot.py`
  - Telegram board formatting now shows bid / ask data instead of `active`
- `tests/test_yuyutei_parser.py`
  - buy-page parser now captures `priceup` and the previous buy price

## Static Verification

Command:

```powershell
C:\AI_Related\codex_work_space\.venv\Scripts\python.exe -m compileall C:\AI_Related\codex_work_space\src
```

Result:

- success
- no syntax errors in `src/`

## Live Verification

### Liquidity boards

Command:

```powershell
@'
from assistant_runtime import get_settings, build_ssl_context
from market_monitor.http import HttpClient
from tcg_tracker.hot_cards import TcgHotCardService

settings = get_settings()
service = TcgHotCardService(
    HttpClient(
        user_agent=settings.yuyutei_user_agent,
        ssl_context=build_ssl_context(settings),
    )
)

for board in service.load_boards(limit=5):
    print("BOARD", board.game, len(board.items))
    for item in board.items[:5]:
        print(
            item.rank,
            item.title,
            "bid", item.best_bid_jpy,
            "ask", item.best_ask_jpy,
            "ratio", item.bid_ask_ratio,
            "liq", item.hot_score,
            "attn", item.attention_score,
        )
'@ | C:\AI_Related\codex_work_space\.venv\Scripts\python.exe -X utf8 -
```

Observed result on 2026-04-17:

- Pokemon board returned `5` items in the sampled run
- WS board returned `5` items in the sampled run
- both boards returned visible `bid`, `ask`, and `bid/ask` data
- both boards also surfaced explicit store-side buy-up signals where available
- both boards still rendered after Cardrush triggered a Python-side `403`, because the existing `curl.exe` fallback path recovered the page

Sample top entries observed:

- Pokemon:
  - `ロケット団のミュウツーex`
  - `best_bid_jpy=2800`
  - `previous_bid_jpy=1400`
  - `best_ask_jpy=3980`
  - `bid_ask_ratio=0.7035`
  - `buy_signal_label=priceup`
- WS:
  - `ヒンデンブルク(サイン入り)`
  - `best_bid_jpy=50000`
  - `previous_bid_jpy=25000`
  - `best_ask_jpy=59800`
  - `bid_ask_ratio=0.8361`
  - `buy_signal_label=priceup`

### Lookup sanity checks

Command:

```powershell
@'
from assistant_runtime import get_settings
from openclaw_adapter.commands import lookup_card

settings = get_settings()
examples = [
    dict(game="pokemon", name="リザードンex", card_number="349/190", rarity="SAR", set_code="sv4a"),
    dict(game="ws", name="ワンダーランドのセカイ 初音ミク", card_number="PJS/S91-T51", rarity="TD", set_code="pjs"),
]

for example in examples:
    result = lookup_card(db_path=settings.monitor_db_path, persist=False, **example)
    print(example["game"], example["name"], len(result.offers), None if result.fair_value is None else result.fair_value.amount_jpy)
'@ | C:\AI_Related\codex_work_space\.venv\Scripts\python.exe -X utf8 -
```

Observed result on 2026-04-17:

- Pokemon `リザードンex 349/190 SAR sv4a`
  - lookup succeeded
  - multiple offers were returned
  - `fair_value=37800`
- WS `ワンダーランドのセカイ 初音ミク PJS/S91-T51`
  - lookup succeeded
  - `fair_value=80`

## Notes

- Listing count is still parsed and retained because it is useful debugging context, but it is no longer used as the primary liquidity score.
- The current version uses buy-side support because recent one-month transaction counts are not exposed uniformly for both Pokemon and WS on a stable public page we can reuse today.
- Explicit store-side buy-up labels are only used when they are tied to a numeric buy quote and a previous quote. Generic promotional copy is still ignored.
- A full dashboard payload build can be slower than the direct board loader because it also loads runtime stats, tools, source catalog data, and the two live boards together.
