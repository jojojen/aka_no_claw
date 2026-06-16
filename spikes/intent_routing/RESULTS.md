# Intent routing spike — LLM router vs bge-m3 embedding router

- cases: **20** real natural-language utterances across 20 distinct intents
- embedding router: **bge-m3** nearest-intent (cosine over canonical phrasings)
- LLM router (production): **qwen3:14b** JSON-schema generation, temperature 0
- scope: routing decision only (which command). Slot-filling NOT measured here.

## Summary

| router | hit@1 | hit@3 | MRR | median latency | p90 latency |
|---|---|---|---|---|---|
| bge-m3 embedding | 20/20 | 20/20 | 1.000 | 130 ms | 281 ms |
| qwen3:14b LLM | 20/20 | — | — | 53651 ms | 56650 ms |

Embedding median latency is **~412x** faster than the LLM router.

## Per-utterance

| # | utterance | expected | embed pred | score | LLM pred | LLM ms | emb ms |
|---|---|---|---|---|---|---|---|
| 1 | 幫我看一下寶可夢 リザードンex 201/165 SAR 現在大概多少 | lookup_card | lookup_card ✅ | 0.709 | lookup_card ✅ | 58791 | 75 |
| 2 | 遊戲王最近哪幾張卡最熱 給我前五名 | trend_board | trend_board ✅ | 0.768 | trend_board ✅ | 74519 | 336 |
| 3 | 在 mercari 幫我盯 初音ミク SSP 低於五萬就通知 | add_watch | add_watch ✅ | 0.829 | add_watch ✅ | 53714 | 162 |
| 4 | 看一下我現在追蹤了哪些東西 | list_watches | list_watches ✅ | 0.869 | list_watches ✅ | 52257 | 140 |
| 5 | 不要再追蹤 a1b2c3d4 了 | remove_watch | remove_watch ✅ | 0.732 | remove_watch ✅ | 54320 | 124 |
| 6 | 把 a1b2c3d4 的價格上限調成 4萬 | update_watch_price | update_watch_price ✅ | 0.710 | update_watch_price ✅ | 53681 | 194 |
| 7 | 這個賣家評價如何 https://jp.mercari.com/item/m98765432 | reputation_snapshot | reputation_snapshot ✅ | 0.881 | reputation_snapshot ✅ | 53715 | 212 |
| 8 | 最近寶可夢卡是不是在跌啊 | web_research | web_research ✅ | 0.994 | web_research ✅ | 53149 | 110 |
| 9 | hunt 清單第 2 個我沒興趣 拿掉 | opportunity_remove | opportunity_remove ✅ | 0.757 | opportunity_remove ✅ | 53621 | 128 |
| 10 | 幫我追蹤推特帳號 @pokeca_new_card | sns_add_account | sns_add_account ✅ | 0.822 | sns_add_account ✅ | 56650 | 168 |
| 11 | 新增一個 X 關鍵字監控 葬送のフリーレン | sns_add_keyword | sns_add_keyword ✅ | 0.934 | sns_add_keyword ✅ | 53899 | 498 |
| 12 | 給我Sns監控清單 | sns_list | sns_list ✅ | 0.848 | sns_list ✅ | 54723 | 281 |
| 13 | 取消追蹤 @elonmusk | sns_delete | sns_delete ✅ | 0.976 | sns_delete ✅ | 52551 | 163 |
| 14 | reddit 上最近大家在聊什麼 one piece 卡 | sns_buzz | sns_buzz ✅ | 0.759 | sns_buzz ✅ | 52571 | 129 |
| 15 | 把 @ARS_Arsales 的篩選關鍵字全部清掉 但保留追蹤 | sns_clear_filter | sns_clear_filter ✅ | 0.831 | sns_clear_filter ✅ | 52216 | 112 |
| 16 | 你這個機器人到底能幫我做什麼 | help | help ✅ | 0.872 | help ✅ | 51634 | 103 |
| 17 | 現在跑得還正常嗎 用的是哪個模型 | status | status ✅ | 0.687 | status ✅ | 51792 | 115 |
| 18 | 把所有可以用的工具列出來給我 | tools | tools ✅ | 0.924 | tools ✅ | 51939 | 113 |
| 19 | 我想用拍照的方式查卡 要怎麼弄 | scan_help | scan_help ✅ | 0.876 | scan_help ✅ | 52032 | 111 |
| 20 | 每個跟 pokemon 有關的追蹤帳號 filter 都幫我加上「抽選」 | sns_bulk_add_filter | sns_bulk_add_filter ✅ | 0.940 | sns_bulk_add_filter ✅ | 55212 | 131 |

## Analysis

**Accuracy is a tie (20/20 each) — the story is latency.**

- qwen3:14b is a *thinking* model: it reasons before emitting the schema-constrained
  JSON, so every route costs ~50–74 s. Production timeout is `OPENCLAW_LOCAL_TEXT_TIMEOUT_SECONDS=75`,
  so case #2 (74.5 s) sits right at the edge — occasionally a natural-language message
  will blow the timeout and silently fall back to the regex router.
- bge-m3 routing is one embedding call + 20 cosine maxes: **median 130 ms**.
- So today, a user who types "給我Sns監控清單" instead of `/snslist` waits the better
  part of a minute. That is the real cost the embedding path removes.

**What this spike does and does NOT prove:**

- It measures the *routing decision* (which command), where embedding ties the LLM at
  far lower latency. The near-pairs that usually trip routers up — `sns_list` (#12, 0.848)
  vs `sns_delete` (#13, 0.976) vs `sns_clear_filter` (#15, 0.831) — separated cleanly.
- It does NOT measure **slot-filling**. The LLM router also extracts price, card number,
  rarity, @handle, schedule minutes, bulk domain, etc. Embedding gives only the label.
  For the ~12 parametrized intents you STILL need extraction.
- Lowest correct score was 0.687 (`status`); every wrong-intent score sat below the
  correct one with margin. A confidence floor (~0.6) cleanly separates confident hits
  from "fall through to LLM / ask to clarify".
- 20 well-formed cases can't prove robustness on adversarial / garbled real input.

## Recommendation

Use embedding as a **fast-path + tie-breaker in front of the existing LLM router**, not
a replacement:

1. Embed ~25 canonical phrasings per intent once at boot (bge-m3 already wired for KB).
2. On each non-slash message, embed it and take the top intent + score.
3. If top score ≥ floor AND the intent is **zero-arg** (`sns_list`, `list_watches`,
   `status`, `tools`, `help`, `scan_help`, `trend_board`-default): short-circuit, skip
   the LLM → ~130 ms instead of ~50 s.
4. Otherwise (low confidence, or a **slot-bearing** intent like `add_watch` /
   `lookup_card` / `sns_add_account`): fall through to the qwen3:14b router exactly as
   today, so slot extraction is unchanged.

Net effect: the cheap, frequent "show me X" commands get near-instant; the parametrized
commands keep full LLM extraction. Main cost: a second routing path to keep in sync, and
a floor to tune so near-pairs never mis-fire (the spike's margins suggest ~0.6 is safe).
Fully reversible — delete the fast-path and every message routes through the LLM as now.

