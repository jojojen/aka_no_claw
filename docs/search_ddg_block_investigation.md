# /search DuckDuckGo Failure — Investigation Notes (historical)

Status: **RESOLVED 2026-06-07.** `/search` now uses Yahoo Japan via a persistent
Playwright Chromium session (`web_search.py::search_yahoo_japan_playwright`);
DuckDuckGo is abandoned. Operating constraint: automated daily search volume
must stay in the single digits to avoid IP blocks. The notes below are kept as
the historical diagnosis (2026-06-03) only.

## TL;DR

`/search` returns 0 results because of **two stacked problems** on the Mac Mini:

1. **The host's IPv6 is broken.** Any IPv6 connection attempt times out
   (even `curl -6 https://www.google.com` fails). Python `urllib` / default
   `curl` try IPv6 first for DuckDuckGo and hang until timeout → the "0 bytes /
   connection timed out" symptom. This masked problem #2.
2. **Over IPv4, DuckDuckGo serves an anomaly / bot-challenge page.** Forcing
   IPv4 reaches DDG fine (`curl -4` connects in <1s) but the `html` endpoint
   returns **HTTP 202** with a body containing `anomaly` ×47, `challenge` ×12,
   `bot` ×4 and **zero `result__a`** markers. So even with IPv6 fixed, DDG
   blocks this IP's scraping at the application layer.

→ Forcing IPv4 alone will NOT restore search. DuckDuckGo scraping is genuinely
bot-blocked for this host. The durable fix is to **swap the search backend.**

## Evidence (commands run)

| Test | Result |
|---|---|
| `curl https://html.duckduckgo.com/html/?q=test` (default) | connection timed out (IPv6 path) |
| `curl -4 https://html.duckduckgo.com/html/?q=test` | **HTTP 202, 0.77s**, body = anomaly/challenge page, 0 `result__a` |
| `curl -6 https://www.google.com/` | **fails to connect** → host IPv6 is broken |
| `curl https://www.google.com/` / `example.com` / `bing.com` | 200 (these picked IPv4 via Happy Eyeballs) |
| system resolver `dig duckduckgo.com` | `status: REFUSED` (the IPv6 NTT/OCN resolver `2404:1a8:7f01::`) |
| `dig @8.8.8.8 duckduckgo.com` | `20.43.161.105` (single Azure IP) |
| Python `search_duckduckgo` with `socket.getaddrinfo` forced to IPv4 | 0 results (got the 202 challenge page) |

So: UA (Chrome vs OpenClaw), Referer, Accept-Language, GET vs POST — **none
matter.** The original "parser broke on `result__a`/`result__snippet`"
hypothesis is half-right: over IPv4 the parser DOES run but finds nothing
because DDG returns a challenge page, not results.

## Backend bake-off (over IPv4)

| Backend | Result | Verdict |
|---|---|---|
| **Bing** HTML scrape | 200 / 70KB but **0** `b_algo`/`b_title` markers; Bing Search API retired 2025 (no free API) | fragile, high-maintenance |
| **SearXNG** searx.be JSON | **403 Forbidden** | public instance blocks bots |
| SearXNG tiekoetter / inetol / priv.au | **429 Too Many Requests** | public instances rate-limit/disable JSON |

## Recommended fixes (pick one — needs user decision)

1. **Brave Search API (free tier)** — *lowest effort, lowest maintenance.*
   Official JSON API, free tier = 2,000 queries/month. The agent's automated
   budget is now ~8 searches/day ≈ 240/month → fits free tier with huge margin.
   Implementation = add `BRAVE_API_KEY` env + one HTTP call with
   `X-Subscription-Token` header, swap `search_duckduckgo` for a
   `search_brave` function. No infra to run.
2. **Self-hosted SearXNG** (Docker on the Mac Mini) — robust, fully
   self-hosted, no third-party key. Enable JSON `format`, point the bot at
   `http://127.0.0.1:<port>/search?format=json`. Aggregates Google/Bing
   server-side so no per-IP bot-block. Maintenance = keep the container updated.
3. **Fix host IPv6** (separately, regardless of backend) — broken IPv6 will add
   Happy-Eyeballs latency to every dual-stack target. Either repair the IPv6
   route or set the bot's HTTP client to force IPv4 (`AF_INET`) as a baseline.

## What was already shipped alongside this (2026-06-03)

- **Daily search budget**: `opportunity_web_search_daily_budget` (default 8) —
  a `DailyCallBudget` shared across all automated DDG entry points (trend sweep,
  candidate enrichment, SNS discovery). Caps automated searches to a
  single-digit daily count. Paired with
  `opportunity_web_search_min_interval_seconds` (now 24h) so the trend sweep
  runs once/day and replays cached candidates between runs.
- **`/fetch <url> <prompt>`** remains the working manual fallback (hits target
  pages directly; unaffected by the DDG block).
- **Poll-loop heartbeat beacon** (price_monitor_bot): a slow `/search` no longer
  trips the watchdog into a false restart.

## Next action when resumed

Decide backend (Brave API vs self-hosted SearXNG), implement the swap behind the
existing `search_fn` injection points in `opportunity_agent.py` (trend provider,
`WebOpportunityResearcher`, SNS discovery) and `telegram_bot.py`
(`default_web_research_renderer`). The `DailyCallBudget` wrapper and `search_fn`
seams already make this a localized change.
