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

Routing rules:

- Return `help` for capability or usage questions.
- Return `status` for runtime/model/service-state questions.
- Return `tools` when the user explicitly wants a tool catalog.
- Return `scan_help` when the user asks how to use image/photo scan or wants image lookup instructions before sending a photo.
- Return `lookup_card` for one-card valuation requests.
- Return `trend_board` for hot/trending/liquidity/ranking requests.
- Return `reputation_snapshot` when a Mercari URL is provided for seller/item trust checking.
- Return watch intents for tracking requests.
- Return `unknown` when the request is unrelated or too ambiguous.
