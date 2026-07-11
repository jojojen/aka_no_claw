# Cross-Repo Contracts Inventory

Status: Current
Owner area: architecture
Last reviewed: 2026-07-12

## 1. Contract Inventory

| Contract | Producer | Consumers | Transport | Current Version | Compatibility Policy | Current Failure Semantics | Fixtures | Migration Owner |
|---|---|---|---|---|---|---|---|---|
| monitor DB read model | `price_monitor_bot` (src/market_monitor/storage.py `MonitorDatabase`) | `aka_no_claw`, SNS interest profile (sns_monitor/interest_profile.py) | SQLite file (RO via URI) | schema v1 (`PRAGMA user_version`, D2.3) | user_version 0 = legacy v0, accepted; >1 = incompatible | `probe_monitor_db()` (sns_monitor_bot, pinned to a locally-known contract version since the dependency doesn't run that direction) distinguishes unavailable/incompatible/corrupt/ok; `_query_watchlist` logs WARNING with state on OperationalError instead of silently returning empty | price_monitor_bot/tests/test_schema_version.py, sns_monitor_bot/tests/test_schema_version.py | done (producer D2.3; consumer probe D2.3 follow-up) |
| SNS DB/inbox | `sns_monitor_bot` (sns_monitor/storage.py `SnsDatabase`) | `aka_no_claw` (opportunity agent, interest profile) | SQLite file | schema v1 (`PRAGMA user_version`, D2.3) | user_version 0 = legacy v0, accepted; >1 = incompatible | `probe_sns_db()` distinguishes unavailable/incompatible/corrupt/ok; interest_profile feedback reader AND aka_no_claw's `SnsLlmCandidateProvider._read_recent_posts` (imports `probe_sns_db` directly — real dependency direction) both log WARNING with state on OperationalError (no longer silent) | sns_monitor_bot/tests/test_schema_version.py, aka_no_claw tests/test_schema_version.py | done (D2.3 + D2.3 follow-up) |
| opportunity DB/inbox | `aka_no_claw` opportunity agent (src/openclaw_adapter/opportunity_store.py `OpportunityStore`) | Telegram commands, dashboard, SNS interest profile reader | SQLite file | schema v1 (`PRAGMA user_version`, D2.3) | user_version 0 = legacy v0, accepted; >1 = incompatible | Write-owned by aka_no_claw; `probe_opportunity_db()` (sns_monitor_bot) distinguishes unavailable/incompatible/corrupt/ok; `_query_pinned_targets` logs WARNING with state on OperationalError instead of silently returning empty | tests/test_schema_version.py, tests/test_opportunity_agent.py, sns_monitor_bot/tests/test_schema_version.py | done (producer D2.3; consumer probe D2.3 follow-up) |
| knowledge DB/inbox | `aka_no_claw` knowledge service (src/openclaw_adapter/knowledge_db.py `KnowledgeDatabase`) | SNS classifier (read), `/knowledge` command (write) | SQLite file | schema v1 (`PRAGMA user_version`, D2.3) | user_version 0 = legacy v0, accepted; >1 = incompatible | All consumers are in-process within aka_no_claw (import `KnowledgeDatabase` directly, not a raw foreign-repo SQLite path) — schema mismatches surface as regular Python exceptions inside the owning repo's own tests, not as a silent-degrade cross-repo boundary. No separate probe needed; not part of the D2.3 follow-up scope | tests/test_schema_version.py | done (producer, D2.3; no consumer probe needed — not a cross-repo raw-path read) |
| reputation HTTP/proof | `reputation_snapshot` (app.py Flask routes, services/proof_service.py `build_proof`, PROOF_VERSION="v0.1") | `aka_no_claw` (`/snapshot` command, opportunity reputation checks) | HTTP JSON + signed payload | envelope v1 (`envelope_version` on every JSON response, D2.4) + PROOF_VERSION v0.1 on signed payload | Missing envelope_version = legacy v0, accepted (compat window); unsupported version → client raises `IncompatibleEnvelopeError` | Client distinguishes: malformed JSON → `CorruptResponseError`; HTTP 429 → `RateLimitedError`; unsupported envelope → `IncompatibleEnvelopeError`; proof expiry → verify returns status "expired" | reputation_snapshot/tests/test_app_api.py, aka tests/test_reputation_snapshot.py | done (D2.4) |
| Telegram registry/hooks | `telegram_core` (contracts.py `RegisteredCommand`, processor.py dispatcher) | `price_monitor_bot`, `aka_no_claw` (via adapter) | Python data class + callback API | `REGISTRY_CONTRACT_VERSION = 1` (contracts.py, D2.5) | Registration validated at construction: builtin-command collision and unusable callback prefixes (empty / containing ':') raise ValueError | Unknown callback prefix → WARNING log + graceful "未知按鈕" answer (verified: no KeyError path); registered-handler-over-builtin-prefix override remains intentional | telegram_core/tests/test_registry_validation.py | done (D2.5) |
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

This inventory (D2.1) established the baseline; D2.3–D2.5 have since landed:

- **D2.3**: all four SQLite producers (`MonitorDatabase`, `SnsDatabase`, `OpportunityStore`, `KnowledgeDatabase`) stamp `PRAGMA user_version = 1` on bootstrap (never downgrade; 0 = legacy v0, still accepted). Exemplar consumer probe `probe_sns_db()` in sns_monitor/interest_profile.py distinguishes unavailable/incompatible/corrupt/ok, and the feedback reader logs a WARNING with the state name instead of silently returning empty.
- **D2.3 follow-up**: the exemplar probe pattern was generalized to the two other foreign-repo raw-SQLite readers. sns_monitor_bot's `interest_profile.py` gained `probe_monitor_db()`/`probe_opportunity_db()` (pinned to a locally-known contract version, since sns_monitor_bot cannot import price_monitor_bot's or aka_no_claw's `SCHEMA_VERSION` — the dependency runs the other way); `_query_watchlist`/`_query_pinned_targets` now log WARNING with resolved state instead of silently returning empty. aka_no_claw's `SnsLlmCandidateProvider._read_recent_posts` now imports `probe_sns_db` directly (a real dependency, unlike the reverse) and does the same. The knowledge DB reader was checked and found to be in-process only (every consumer imports `KnowledgeDatabase` directly within aka_no_claw) — not a cross-repo raw-path read, so no separate probe applies there.
- **D2.4**: reputation_snapshot stamps `envelope_version: 1` on every JSON response (after_request hook; the field is transport-only and stripped before signature verification). The aka client raises distinct `IncompatibleEnvelopeError` / `CorruptResponseError` / `RateLimitedError`; missing envelope_version is accepted as legacy v0 during the compatibility window.
- **D2.5**: telegram_core exports `REGISTRY_CONTRACT_VERSION = 1` and validates registries at construction: registering a builtin command name (would be silently shadowed) or an unusable callback prefix (empty / containing ':') raises ValueError. The previously documented "missing handler → KeyError" claim was found incorrect on inspection — unknown prefixes already log a WARNING and answer gracefully.

Remaining (tracked in issue #77):

- Command-bridge JSON/SSE envelope versioning (D2.4 scope covered reputation HTTP only).

This doc will be updated as that follow-up completes.
