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

- `/price <game> <name>`
- `/price <game> | <name> | <card_number> | <rarity> | <set_code>`
  - Look up the price/value of one card.
  - Games: `pokemon`, `ws`.

- `/trend <game> [limit]`
- `/hot <game> [limit]`
- `/liquidity <game> [limit]`
  - Show hot / trending / liquidity boards.
  - Games: `pokemon`, `ws`.
  - Limit should usually be 1-10, default 5.

- `/snapshot <url>`
  - Build or reuse a reputation snapshot for a Mercari item/profile URL.

- `/search <question>`
- `/research <question>`
  - Search the web with DuckDuckGo, then summarize the result with the configured local LLM.
  - Include source URLs / references in the final reply.
  - Use when the user asks a general explanatory or background question that needs sources, such as why a TCG card is popular.

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

- `/snsadd @username`
  - Add an X (Twitter) account to the watch list.
  - Usage: `/snsadd @akanoclaw`
  - Optional account tweet filters: `/snsadd @elonmusk ["buy", "sell"]` only notifies when the account tweet contains any listed keyword.

- `/snsadd keyword:<search_term>`
  - Add a keyword search to the watch list.
  - Usage: `/snsadd keyword:機動戰士`

- `/snsadd trend:<category>`
  - Add a trend category to the watch list.
  - Categories: `trending`, `for-you`, `news`, `sports`, `entertainment`
  - Usage: `/snsadd trend:trending`

- `/snslist`
  - List all X (Twitter) watch rules.

- `/snsdelete <target>`
  - Remove an X watch rule. `<target>` may be a rule ID prefix, an @handle, or `keyword:xxx`.
  - Examples: `/snsdelete @elonmusk`, `/snsdelete abc12345`, `/snsdelete keyword:機動戰士`

- `/snsbuzz <keyword>`
  - Summarise Reddit's top discussion on a topic via LLM.
  - Usage: `/snsbuzz amd`

- `/hunt status`
  - Show recent opportunity candidates and recommendation decisions.

- `/hunt remove <number-or-name>`
  - Remove/dismiss an opportunity target from the active hunt list.
  - Use when the user says they are not interested in a target from `/hunt status`.
  - `<number-or-name>` may be the visible list number from `/hunt status` or part of the product name.

Natural-language examples for SNS intents (router must distinguish these from Mercari watch intents — any `@handle` or X/Twitter/推特 keyword forces SNS):

- "追蹤 @elonmusk" → `sns_add_account`, sns_handle="elonmusk"
- "刪除追蹤 @elonmusk" / "取消追蹤 @elonmusk" / "unfollow @elonmusk" → `sns_delete`, sns_handle="elonmusk"
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
- Return `trend_board` for hot/trending/liquidity/ranking requests.
- Return `reputation_snapshot` when a Mercari URL is provided for seller/item trust checking.
- Return `web_research` when the user asks an explanatory/background question that needs web sources and summarization.
- Return `opportunity_remove` when the user wants to remove/dismiss a target from the opportunity/hunt list.
- Return watch intents for tracking requests.
- Return `unknown` when the request is unrelated or too ambiguous.
