# Liquidity Methodology

Last updated: 2026-04-17

This file explains how the dashboard's `Pokemon Liquidity Board` and `WS Liquidity Board` are ranked.

## 1. What We Mean By Liquidity

In this project, liquidity means:

- a card can be bought or sold quickly
- the gap between sell-side and buy-side quotes is not too wide
- there is credible evidence that somebody wants to buy now, not just list now

We intentionally do **not** treat listing count as the liquidity score.

## 2. Why Listing Count Was Removed

`active listings`, `在庫数`, and `出品数` are supply-side snapshots.

They can be useful as context, but they do not reliably answer:

- how much traded recently
- how fast a holder can exit
- whether buyers are actually waiting near the current ask

A card can have many listings because it is:

- overpriced
- stale
- duplicated across condition variants
- being flipped repeatedly without clearing

That makes listing count a weak primary liquidity metric.

## 3. Current Scoring Model

The current implementation prefers a cross-game signal we can observe consistently on public pages: **buy-side support**.

### 3.1 Candidate Discovery

Source pages are only used to discover candidate cards.

- Pokemon:
  - primary candidate page: Cardrush Pokemon high-rarity singles
  - fallback candidate page: magi Pokemon page
- WS:
  - primary candidate page: magi Weiss Schwarz page

These pages help us find what to evaluate, but their listing counts do **not** drive the ranking.

### 3.2 Primary Liquidity Signal: Buy-Side Support

For each candidate card, we look up matching quotes on 遊々亭 and derive:

- whether a credible buylist quote exists
- the best visible ask quote
- the best visible bid quote
- the `bid / ask` ratio
- whether the store explicitly shows the buy quote was raised

The `buy_support_ratio` is:

```text
0.35 if a bid exists
+ 0.50 * min(1.0, bid / ask) when both bid and ask exist
+ 0.15 when both bid and ask exist
+ small momentum boost when the store explicitly marks the bid as raised
```

So the strongest cards are the ones where:

- a buy quote exists
- the buy quote is close to the visible ask
- the market looks two-sided instead of one-sided
- the store is visibly increasing its buy price, which is a stronger signal than generic marketing copy

### 3.2.1 Explicit Store-Side Buy-Up Signal

If a store page exposes something stronger than a slogan, we use it.

Current example:

- 遊々亭 buy pages sometimes mark a card block with `priceup`
- the same block also shows the previous buy price in a struck-through `<del>` price

When that happens, we treat it as a real store-side buy-pressure signal because:

- it is attached to a concrete card
- it is tied to an actual numeric buy quote
- we can compare old bid vs current bid directly

This is treated as an auxiliary reference signal only.
It gets only a modest boost, helps break close calls, and does not overpower the core bid / ask relationship.

### 3.3 Liquidity Score

The board's `liquidity_score` is:

```text
liquidity_score = buy_support_ratio * 90 + fungibility_ratio * 10
```

Where:

- `fungibility_ratio = 1.0` for raw copies
- `fungibility_ratio = 0.7` for graded copies

Graded cards are penalized slightly because they are usually less interchangeable than raw copies.

### 3.4 Attention Score

`attention_score` is not the liquidity score.

It is a side channel used after liquidity to break ties and add demand context.

Right now it uses:

- Yahoo!リアルタイム検索 matched public posts
- visible engagement on matched posts

If social signal is missing, the attention score drops to zero. That does not automatically remove a card from the board if buy-side support is strong.

## 4. Ranking Order

Current board ordering is:

1. `liquidity_score`
2. `buy_support_score`
3. `attention_score`
4. raw before graded
5. candidate-page rank as a late tie-breaker

`listing_count` is kept only as background context in notes. It is not part of the score.

## 5. Why This Is More Reasonable

This model is closer to practical liquidity because it asks:

- is there an actual buyer quote
- how close is that buyer quote to the seller quote
- can we see two-sided market support right now

That is more actionable than simply counting how many offers are visible.

Conceptually this aligns better with standard liquidity dimensions such as immediacy and tightness than a raw listing counter alone.

## 6. Current Limits

This is better than listing count, but it is still a proxy.

Current limits:

- recent one-month transaction counts are not exposed uniformly for both Pokemon and WS on stable public pages we can reuse right now
- 遊々亭 buylist quotes reflect dealer demand, not the entire market
- explicit store-side buy-up labels are useful, but they still reflect one venue's demand rather than the whole market
- Yahoo!リアルタイム検索 is useful for attention context, but social chatter can be noisy
- candidate discovery still begins from ranking / listing pages, even though those pages no longer define the score itself

## 7. Planned Upgrades

The next better version would add one or more of these:

- recent monthly transaction count feeds where a stable public source exists
- sold-history data from marketplaces with reliable public access
- cross-source buy-side support instead of relying on one store's buylist
- source-diversity bonuses when multiple credible venues agree

## 8. Source References

- 遊々亭 buy guide:
  - https://img.yuyu-tei.jp/sp/info/buy_10.php
- Yahoo!リアルタイム検索:
  - https://search.yahoo.co.jp/realtime
- IMF discussion of liquidity measurement:
  - https://www.imf.org/en/Publications/WP/Issues/2016/12/30/Measuring-Liquidity-in-Financial-Markets-16211
- Example of recent-month transaction ranking pages on SNKRDUNK that may become future inputs:
  - https://snkrdunk.com/articles/31649/
