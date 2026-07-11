# Cross-Repo Contracts Inventory

Status: Current
Owner area: architecture
Last reviewed: 2026-07-11

## 1. Contract Inventory

| Contract | Producer | Consumers | Transport | Current Version | Compatibility Policy | Current Failure Semantics | Fixtures | Migration Owner |
|---|---|---|---|---|---|---|---|---|
| monitor DB read model | `price_monitor_bot` (src/market_monitor/storage.py `MonitorDatabase`) | `aka_no_claw`, SNS interest profile (sns_monitor/interest_profile.py) | SQLite file (RO via URI) | none (implicit v0) | none yet (D2.3 target) | Missing DB → empty tuple; OperationalError (schema mismatch) → empty tuple; silently degrades (violates D2.2 rule) | price_monitor_bot/tests/fixtures | TBD (D2.3–D2.5) |
| SNS DB/inbox | `sns_monitor_bot` (sns_monitor/storage.py `SnsDatabase`) | `aka_no_claw` (opportunity agent, interest profile) | SQLite file | none (implicit v0) | none yet (D2.3 target) | RO access fails gracefully to empty; OperationalError on missing tables → empty (implicit empty = unavailable, not distinguished) | tests/fixtures | TBD (D2.3–D2.5) |
| opportunity DB/inbox | `aka_no_claw` opportunity agent (src/openclaw_adapter/opportunity_store.py `OpportunityStore`) | Telegram commands, dashboard, SNS interest profile reader | SQLite file | none (implicit v0) | none yet (D2.3 target) | Write-owned by aka_no_claw; read failures degrade gracefully; missing table → empty query result | tests/test_opportunity_agent.py | TBD (D2.3–D2.5) |
| knowledge DB/inbox | `aka_no_claw` knowledge service (src/openclaw_adapter/knowledge_db.py `KnowledgeDatabase`) | SNS classifier (read), `/knowledge` command (write) | SQLite file | none (implicit v0) | none yet (D2.3 target) | Read failures on missing/corrupt tables → empty entries or aliases; no distinction between missing and parse error (silent degradation) | TBD (D2.3–D2.5) | TBD (D2.3–D2.5) |
| reputation HTTP/proof | `reputation_snapshot` (app.py Flask routes, services/proof_service.py `build_proof`, PROOF_VERSION="v0.1") | `aka_no_claw` (`/snapshot` command, opportunity reputation checks) | HTTP JSON + signed payload | v0.1 (on proof envelope only) | Old v0.1 proofs accepted during 30-day expiry window; no deprecation path beyond expiry | Network/HTTP errors not explicitly caught; malformed JSON → parse error; proof expiry → silent rejection (treated as unavailable) | reputation_snapshot/tests/fixtures | TBD (D2.3–D2.5) |
| Telegram registry/hooks | `telegram_core` (contracts.py `RegisteredCommand`, processor.py dispatcher) | `price_monitor_bot`, `aka_no_claw` (via adapter) | Python data class + callback API | none (implicit v0) | none yet (D2.5 target); currently inline callback registration at startup | Callback prefix collision not detected; missing handler → KeyError at dispatch time; no versioning on hook format | TBD (D2.3–D2.5) | TBD (D2.3–D2.5) |
| command bridge JSON/SSE | `aka_no_claw` (src/openclaw_adapter/command_bridge.py; JSON request/SSE response envelopes) | `aka_no_claw_web` frontend | HTTP POST JSON + Server-Sent Events (streaming) | none (implicit v0) | none yet (D2.4 target) | Malformed SSE → client buffer truncation; timeout on missing result → socket hang; no explicit version in envelope | TBD (D2.3–D2.5) | TBD (D2.3–D2.5) |

## 2. Failure Vocabulary

Every boundary must distinguish these seven states:

- **unavailable**: optional source absent, offline, or unreachable (e.g., reputation service down, opportunity DB file missing). Caller degrades gracefully.
- **empty**: compatible source returned zero records (e.g., watchlist has no active queries). Semantically different from unavailable; signals "I tried and found nothing."
- **stale**: compatible data present but outside freshness policy (e.g., proof expired beyond 30-day window). Caller knows to refetch/invalidate.
- **incompatible**: unsupported contract/schema version (e.g., v0.2 probe hits v0.0 response). Caller must reject, not interpret.
- **corrupt**: cannot parse or verify (e.g., JSON malformed, signature invalid). Caller must not interpret as empty.
- **rate_limited**: upstream explicitly throttled (e.g., HTTP 429, Telegram rate-limit). Caller retries with backoff.
- **rejected**: policy intentionally refused the operation (e.g., reputation proof revoked, command denied by firewall). Caller is transparent about reason.

**Hard rule**: No `incompatible` or `corrupt` condition may silently become `empty`. All seven states must be observable and actionable by the consumer.

## 3. Migration Sequence (D2.6)

For any breaking boundary change:

1. **Producer dual-emit** (where practical): Accept and emit both old and new versions. Example: reputation_snapshot emits proof v0.1 AND v0.2 side-by-side; opportunity DB reads both schema v0 and v1 tables.
2. **Consumer new-version support**: Consumer code adds new-version parsing/validation without removing old logic. Tests verify both paths.
3. **Compatibility manifest advances**: Update `config/workspace-lock.toml` to note the new minimum supported version across all repos. Record the transition date.
4. **Live behavior verified**: Run cross-repo contract tests (D2.3–D2.5) in CI or on staging to confirm producers and consumers interoperate. No silent fallback.
5. **Old support removed only after compatibility window** (≥14 days per issue #77 guidance): Once all deployed instances have consumed the new version, old parser/emitter code may be deleted. Record the removal date.
6. **Rollback restores previous manifest/version without data loss**: Revert workspace-lock.toml and any schema migrations. Data from the newer version is preserved (not deleted); queries on old code simply ignore new columns/tables.

## 4. Status & Next Steps

This inventory (D2.1) establishes the baseline contract registry with producer/consumer/transport/failure semantics. All seven boundaries currently have:

- **No explicit versioning** (implicit v0); only reputation_snapshot has a version string on the proof envelope.
- **Silent degradation on errors** (e.g., interest_profile silently returns empty tuple on OperationalError) — violating D2.2's hard rule that incompatible/corrupt must not become empty.
- **No fixtures or cross-repo test harness** yet.

**D2.3–D2.5** (tracked separately in issue #77) will add:

- SQLite schema version metadata + validation (D2.3).
- HTTP envelope versioning + old-proof compatibility window (D2.4).
- Telegram registry uniqueness tests + callback-format versioning (D2.5).

This doc will be updated as those workstreams complete.
