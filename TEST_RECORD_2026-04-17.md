# Test Record 2026-04-17

## Scope

This pass verified the hot-card ranking update that broadened Pokemon candidate discovery and changed the board from a mostly buylist-driven proxy to a mixed score:

- recent transaction activity
- buy-side support
- SNS attention

Main intent:

- fix the unreasonable outcome where `ピカチュウ` / `リザードン` could disappear from the top board
- keep listing count as background context only
- preserve Telegram / dashboard compatibility

## Code Areas Under Test

- `src/tcg_tracker/hot_cards.py`
- `tests/test_hot_cards.py`
- `tests/test_telegram_bot.py`
- `README.md`
- `LIQUIDITY_METHODOLOGY.md`

## Automated Tests

### Focused regression pass

Command:

```powershell
C:\AI_Related\codex_work_space\.venv\Scripts\python.exe -m pytest `
  tests/test_hot_cards.py `
  tests/test_telegram_bot.py
```

Result:

```text
23 passed
```

What this covered:

- existing Cardrush / magi parsing still works
- new SNKRDUNK ranking text parsing works
- duplicate variants are still merged
- explicit `priceup` store-side buy signals still boost modestly
- SNS remains secondary
- recent market-activity evidence can now outrank a purely better buylist shape
- Telegram trend commands still format and dispatch correctly

## Dashboard Verification

### pytest note

`tests/test_dashboard.py` is still blocked on this machine by a local Windows directory-permission problem during pytest temp-dir cleanup.

Observed failure mode:

- `PermissionError: [WinError 5]` while pytest tries to remove its temp base directory
- reproduced both with the system temp path and with repo-local `--basetemp`

This is an environment issue, not a dashboard logic regression.

### Equivalent manual verification

The dashboard payload path was verified indirectly by:

- keeping the `HotCardEntry` payload shape unchanged
- preserving `liquidity_score`, `hot_score`, `attention_score`, `best_bid_jpy`, `best_ask_jpy`, and references
- running live board generation and confirming the returned items contain the expected fields consumed by dashboard JSON

## Live Verification

### Pokemon board

Command:

```powershell
@'
from assistant_runtime.settings import load_dotenv
from tcg_tracker.hot_cards import TcgHotCardService

load_dotenv("C:/AI_Related/codex_work_space/.env", override=True)
service = TcgHotCardService()
board = service.load_pokemon_board(limit=10)

print(board.label)
for item in board.items:
    refs = ", ".join(reference.label for reference in item.references[:4])
    print(
        f"#{item.rank} | {item.title} | {item.card_number or '-'} | {item.rarity or '-'} "
        f"| score={item.hot_score:.2f} | bid={item.best_bid_jpy} | ask={item.best_ask_jpy} | refs={refs}"
    )
'@ | C:\AI_Related\codex_work_space\.venv\Scripts\python.exe -X utf8 -
```

Observed result on 2026-04-17:

```text
Pokemon Liquidity Board
#1 | メガリザードンXex | 223/193 | MA | score=102.25 | bid=7000 | ask=3700
#5 | ピカチュウex | 234/193 | SAR | score=96.66 | bid=48000 | ask=30000
#7 | メガリザードンXex | 110/080 | SAR | score=94.98 | bid=120000 | ask=69999
```

Important outcome:

- `ピカチュウ` is back in the live top 10
- `リザードン` is back in the live top 10
- the board is no longer dominated only by whichever cards happen to fit one store's buylist best

### Source evidence used in reasoning

The updated logic was informed by these current pages:

- SNKRDUNK monthly Pokemon trade ranking:
  - `https://snkrdunk.com/articles/31649/`
- SNKRDUNK UR trade ranking:
  - `https://snkrdunk.com/articles/31962/`
- SNKRDUNK SA trade ranking:
  - `https://snkrdunk.com/articles/31708/`

Concrete examples seen on those pages:

- monthly ranking included:
  - `メガリザードンXex MA [M2a 223/193]`
  - `ピカチュウex SAR [M2a 234/193]`
  - `メガリザードンXex SAR [M2 110/080]`
- UR ranking included:
  - `ピカチュウex UR [SV8a 236/187]`
  - `ピカチュウex UR [SV8 136/106]`
  - `リザードンex UR [SV3 139/108]`
- SA ranking included:
  - `リザードンV SA [S9 103/100]`
  - `ピカチュウ&ゼクロムGX SA [SM9 101/095]`

These pages support the conclusion that a board with no Pikachu / Charizard presence was too narrow.

## Notes

- `listing_count` remains in notes only; it is not part of the primary score.
- Current final weighting is:
  - market activity `50%`
  - buy-side support `45%`
  - SNS attention `5%`
- WS still has weaker cross-market activity inputs than Pokemon, because we have not yet found an equally fresh, broad, and parser-stable WS transaction ranking source.
