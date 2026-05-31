# OpenClaw Telegram Tool Spec

The Telegram assistant can reach these tools:

- `/help`
  - Show the supported commands and short examples.
  - Use when the user asks what the bot can do or how to use it.

- `/status`
  - Show runtime status, active models, OCR paths, and reputation agent status.
  - Use when the user asks about current bot state, model usage, or service health.

- `/tools`
  - Show the internal tool catalog.
  - Use when the user explicitly asks for all available tools or capabilities in catalog form.

- `/new <request>`
  - When no fixed tool covers a request, the bot uses the strongest local model
    (qwen3:14b) to WRITE and run a one-off Python tool, reusing a prior generated
    tool when one fits. Local-only, no paid API.
  - Example: `/new 幫我查0050今年以來到5月的年化報酬`.
  - This is an explicit command (handled before NL routing); a dedicated NL
    intent is not yet wired.

- `/price <game> <name>`
- `/price <game> | <name> | <card_number> | <rarity> | <set_code>`
  - Look up the price/value of one card.
  - Games: `pokemon`, `ws`.

- `/trend <game> [limit]`
- `/hot <game> [limit]`
- `/liquidity <game> [limit]`
  - Show the static hot / ranking / liquidity leaderboard for a game.
  - Games: `pokemon`, `ws`.
  - Limit should usually be 1-10, default 5.
  - Use ONLY for explicit leaderboard / top-N / ranking requests. Does NOT analyse price direction or market sentiment — those go to `/search`.

- `/snapshot <url>`
  - Build or reuse a reputation snapshot for a Mercari item/profile URL.

- `/search <question>`
- `/research <question>`
  - Search the web with DuckDuckGo, then summarize the result with the configured local LLM.
  - Include source URLs / references in the final reply.
  - Use for price-direction / market-sentiment / recent-news / why-how questions, e.g. "寶可夢卡是不是在跌", "why are pokemon cards popular", "pokemon 市場最近怎麼了", "遊戲王最近暴跌".

- Photo scan flow
  - Send a photo with caption `/scan pokemon` or `/scan ws`.
  - The bot can also handle a plain card photo without a strict caption and try OCR/vision lookup.
  - Use this path when the user is asking to identify or price a card from an image.

- `/watch <query> on <price>`
  - Add a Mercari watch.

- `/watchlist`
  - Show the current watch list.

- `/unwatch <watch_id>`
  - Remove a watch.

- `/setprice <watch_id> <price>`
  - Update the price threshold of an existing watch.

- `/snsadd <source>:<target> [filter[...] domain[...] schedule:NN]`
  - Add (or upsert) a watch rule. Supported sources:
    - `x:` — X (Twitter) via Nitter RSS. Account form `@handle`, keyword form `keyword:<term>`, trend form `trend:<category>`.
    - `reddit:` — Reddit via public JSON API. Subreddit form `r/<sub>`, keyword form `keyword:<term>`. No trend form.
  - Examples:
    - `/snsadd x:@akanoclaw domain[pokemon]`
    - `/snsadd x:@elonmusk filter[buy, sell] domain[stock] schedule:30`
    - `/snsadd x:keyword:機動戰士 domain[gundam, anime]`
    - `/snsadd x:trend:trending domain[news]`
    - `/snsadd reddit:r/PokemonTCG domain[pokemon] schedule:30`
    - `/snsadd reddit:r/yugioh domain[yugioh] schedule:60`
    - `/snsadd reddit:keyword:Umbreon ex domain[pokemon]`
  - **Backcompat**: a bare `@handle` / `keyword:` / `trend:` (no source prefix) is treated as `x:` so existing scripts keep working.
  - Optional **filter** keywords (post must contain any of them to notify): `filter[抽選, 再販]` or legacy `["抽選", "再販"]`.
  - Optional **domain** tags so the right topic agent can pick up the rule: `domain[pokemon, yugioh]`.
  - Optional **schedule** override (per-rule, in minutes, clamped 5–1440): `schedule:30`. Per-source defaults are X-account=15m, X-keyword=30m, X-trend=60m, Reddit-account=30m, Reddit-keyword=60m.
  - Re-running with the same `<source>:<target>` is an upsert: filter / domain / schedule replace if explicitly provided; otherwise existing values are preserved.

- `/snslist`
  - List all watch rules. Each row shows `[source] <target> filter[…] domain[…] schedule:NNm`; rules awaiting LLM backfill display `domain[?]`.

- `/snsdelete <target>`
  - Remove a watch rule. `<target>` may be a rule ID prefix, an @handle (X), `r/<sub>` (Reddit), `keyword:<term>`, or a source-prefixed form (`x:@elon`, `reddit:r/PokemonTCG`, `reddit:keyword:…`).
  - Examples: `/snsdelete @elonmusk`, `/snsdelete abc12345`, `/snsdelete keyword:機動戰士`, `/snsdelete reddit:r/PokemonTCG`

- Clear filter (natural language only, no slash form)
  - Pattern: `把 @<handle> 的 filter 全部拿掉` / `清空 @<handle> 的篩選` / `clear filter on @<handle>`
  - Action: only clears `include_keywords` on the @handle while keeping the watch rule active. `domains` and other fields are preserved.
  - To remove the rule entirely, use `/snsdelete @<handle>` instead.
  - Idempotent: if there's no filter, bot replies `目前沒有 filter，無需清空`.

### Domain tags

Each watch rule carries a `domains` tuple. Topic-specific agents filter by intersection (TCG agent reads only rules whose `domains` intersect `{pokemon, yugioh, ws, union_arena, tcg}`). Recommended values:

- TCG: `pokemon`, `yugioh`, `ws`, `union_arena`, `tcg`
- Non-TCG: `politic`, `stock`, `news`, `gaming`, `entertainment`, `anime`, `gundam`, `other`

Free-text values are accepted (normalised to lowercase). Untagged rules are auto-tagged by the opportunity agent's LLM backfill, one rule per cron tick — the user receives a Telegram heads-up `🏷 自動標記 @X 領域：…` and can override with `/snsadd @X domain[…]`.

### Bulk filter update (natural language only)

- Pattern: `把每個跟 <domain> 相關的 SNS 追蹤帳號 filter 都加上「<keyword>」`
- Examples:
  - `把每個跟 pokemon 相關的 sns 追蹤帳號 filter 都加上「抽選」`
  - `幫所有 yugioh 帳號加上 新弾 filter`
  - `所有遊戲王帳號的篩選都改成包含 新弾`
- Bot 會列出符合 domain 的帳號 + inline 鍵盤要求二次確認。
- Allowed `<domain>`: `tcg`（=所有 TCG）、`pokemon`、`yugioh`、`ws`、`union_arena`。
- 已含該 filter 的帳號會被跳過（idempotent）。

- `/snsbuzz <keyword>`
  - Summarise Reddit's top discussion on a topic via LLM.
  - Usage: `/snsbuzz amd`

- `/hunt status`
  - Show recent opportunity candidates and recommendation decisions.

- `/hunt remove <number-or-name>`
  - Remove/dismiss an opportunity target from the active hunt list.
  - Use when the user says they are not interested in a target from `/hunt status`.
  - `<number-or-name>` may be the visible list number from `/hunt status`, the candidate id prefix, the product name, OR any of the product's `aliases`.

- `/hunt alias <selector> add <names>` / `/hunt alias <selector> remove <names>`
  - Mutate the `aliases` list on one candidate (same-product synonyms / spellings used to expand Mercari search and `/hunt remove` matching).
  - `<selector>` is the visible list number from `/hunt status` or a candidate-id prefix.
  - `<names>` accepts comma separators (`,` / `，` / `、`); without separators the entire tail is one alias (so multi-word aliases like `テラスタル ピカチュウ sar` work without quoting).
  - Idempotent: re-adding existing aliases is a no-op; removing missing ones doesn't error.
  - Examples:
    - `/hunt alias 2 add Pikachu SAR, テラスタル ピカチュウ sar`
    - `/hunt alias opp_abc12345 remove Terastal Pikachu SAR`

- `/hunt related <selector> add <names>` / `/hunt related <selector> remove <names>`
  - Same shape as `/hunt alias`, but operates on `related_keywords` (different products with market correlation, e.g. an upcoming set that drives demand). `related_keywords` are surfaced to the user and used as LLM/Web-research context but **not** for Mercari search nor `/hunt remove` matching — that would mis-target different products.

Natural-language examples for SNS intents (router must distinguish these from Mercari watch intents — any `@handle` or X/Twitter/推特 keyword forces SNS):

- "追蹤 @elonmusk" → `sns_add_account`, sns_handle="elonmusk"
- "刪除追蹤 @elonmusk" / "取消追蹤 @elonmusk" / "unfollow @elonmusk" → `sns_delete`, sns_handle="elonmusk"
- "把 @ARS_Arsales 的 filter 全部拿掉" / "清空 @elonmusk 的篩選" / "clear filter on @aka_claw" → `sns_clear_filter`, sns_handle="<handle>"
- "我的 X 追蹤清單" / "推主追蹤" → `sns_list`
- "整理一下 amd 最近熱門討論" → `sns_buzz`, sns_buzz_query="amd"
- "remove target 2 from opportunity list" → `opportunity_remove`, opportunity_target="2"
- "I am not interested in Umbreon ex SAR" → `opportunity_remove`, opportunity_target="Umbreon ex SAR"

Routing rules:

- Return `help` for capability or usage questions.
- Return `status` for runtime/model/service-state questions.
- Return `tools` when the user explicitly wants a tool catalog.
- Return `scan_help` when the user asks how to use image/photo scan or wants image lookup instructions before sending a photo.
- Return `lookup_card` for one-card valuation requests.
- Return `trend_board` ONLY for explicit leaderboard / top-N / ranking requests (e.g. "pokemon 熱門前 5", "遊戲王熱門排行"). Price-direction questions go to `web_research`, not `trend_board`.
- Return `reputation_snapshot` when a Mercari URL is provided for seller/item trust checking.
- Return `web_research` when the user asks about price direction, market sentiment, recent news, or any why/how/explanatory question that needs web sources and summarization.
- Return `opportunity_remove` when the user wants to remove/dismiss a target from the opportunity/hunt list.
- Return watch intents for tracking requests.
- Return `unknown` when the request is unrelated or too ambiguous.
