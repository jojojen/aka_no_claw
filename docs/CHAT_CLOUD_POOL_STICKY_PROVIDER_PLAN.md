# Chat Cloud-Pool Sticky Provider Plan

Last reviewed: 2026-07-07
Status: Planned
Owner area: dynamic-tools
Created: 2026-07-07
Origin: ported from musubi-for-tenkyoku issue #11 Case C, P1 ("sticky
provider"), `musubi-for-tenkyoku/docs/issue-11-case-c-structural-improvements-plan.md`
§2.2 — implemented and unit-tested there 2026-07-07 (9 passing tests in
`musubi-for-tenkyoku/tests/sticky-selection.test.ts`). No GitHub issue filed
yet for this aka_no_claw side; file one before implementing if the repo's
workflow requires it.

This doc is written to be implementable by a cold agent with zero prior
context on this conversation: every touch point below cites file + line as
of 2026-07-07. Read `docs/DOCUMENTATION_GOVERNANCE.md` first if you are not
already familiar with this repo's doc rules (this file follows them).

**Goal:** in the web chat's `cloud_pool` backend, once a provider (Gemini /
Mistral / Big Pickle) actually answers a turn, keep using that provider for
the rest of the *same conversation* — re-pinning to whichever provider
actually serves the next request if the pinned one ever fails — instead of
restarting the full chain from Gemini on every single turn regardless of
what happened one turn ago.

---

## 1. Motivation — the gap, verified against real code

### 1.1 Where this idea comes from

musubi-for-tenkyoku (a sibling project at
`/Users/jen/ai_work_space/related_to_claw/musubi-for-tenkyoku`) runs an
agentic browser-automation benchmark (WorkArena) where one worker completes
one task over many LLM-driven steps. Its issue #11 Case C plan identified
that the vision-provider pool was being re-walked from the front on every
single step, even when the exact same provider had just successfully served
the previous nine steps of the same task — wasting latency and quota on
providers already known to be unavailable/rate-limited *for this task*.

The fix ("P1 sticky provider", implemented 2026-07-07): once a provider
serves a step, pin it as the preferred starting point for the rest of that
task; the existing fail-and-fall-through behavior is unchanged, only the
*starting point* of the walk changes. Implementation:
`musubi-for-tenkyoku/packages/server/src/phase6/action-selector.ts`,
`nextVisionProviderOrder(pinned?)` — moves the pinned provider to the front
of the rotation array via splice/unshift, preserving the rest of the
fallback order untouched. The runner re-pins to whichever provider actually
served *every* step (not just the first), so a pinned provider that later
goes down gets naturally replaced by whoever picks up the slack.

While investigating whether `aka_no_claw_web`'s chat feature has the same
structural gap, this exact pattern turned up — see §1.2.

### 1.2 Confirmed gap in this repo (verified 2026-07-07, not assumed)

`aka_no_claw_web` is frontend-only (`ChatBackendSelector.tsx`,
`ChatSettingsModal.tsx`); per its own source comment
(`aka_no_claw_web/frontend/src/types/command.ts:1-3`), "the web app treats
the bridge as an external local API and never reimplements routing." All
actual provider selection lives in **this repo**, `aka_no_claw`, in
`src/openclaw_adapter/command_bridge.py` and
`src/openclaw_adapter/llm_pool_settings.py`.

A rotation primitive already exists —
`CloudPoolRotation` (`llm_pool_settings.py:127-149`):

```python
class CloudPoolRotation:
    """Rotates which cloud-pool provider a chain-walk starts from.

    One goal-loop run shares a single instance across every LLM call it makes
    (draft, each replan, the result judge, each llm_transform step). Without
    rotation every call re-tries provider[0] first, so one long multi-step task
    hammers a single provider's rate limit instead of spreading load across the
    pool; the existing per-call fail-and-fall-through behavior is unchanged,
    only the starting point advances between calls.
    """
    def __init__(self) -> None:
        self._cursor = 0

    def rotate(self, items: Sequence) -> list:
        size = len(items)
        if size == 0:
            return []
        order = [items[(self._cursor + i) % size] for i in range(size)]
        self._cursor = (self._cursor + 1) % size
        return order
```

**But it is only wired into two call paths, both scoped to a single
request/run, neither of which is plain chat's answer generation:**

1. **Goal-loop (`/new`-style multi-step runs)** — `_execute_goal_loop`
   (`command_bridge.py:1935-1973`) creates `pool_rotation = CloudPoolRotation()`
   as a **local variable, fresh every call** (line 1949), shared only by the
   several LLM calls *within that one run* (draft/replan/judge/transform via
   `_build_goal_planner`, `_goal_llm_transform_client`, `_goal_result_judge`
   at lines 1953/1957/1963). It never persists across separate chat turns.
2. **Chat-tool planner sub-call** — `_generate_cloud_pool_chat_tool_plan`
   (`command_bridge.py:2509-2532`) accepts an optional `pool_rotation` and is
   exercised by `test_generate_cloud_pool_chat_tool_plan_rotates_start_provider`
   (`tests/test_command_bridge.py:3754`). This is the *tool-selection planner*
   step, not the final chat answer.

**The actual chat-answer generation — the thing the user sees as "the
reply" — has zero rotation memory, confirmed by reading the exact call
sites:**

- `_handle_chat_blocking` (`command_bridge.py:872`), line 943:
  `message, metadata = self._generate_chat_response_blocking(prompt, req.chat_backend)`
  — no `pool_rotation`, no conversation identifier passed at all.
- `_stream_chat` (`command_bridge.py:948`), line 1005:
  `yield from self._stream_chat_response(prompt, req.chat_backend)` — same.
- `_generate_chat_response_blocking` (`command_bridge.py:4036-4074`), line
  4066: `if chat_backend == CHAT_BACKEND_CLOUD_POOL: return
  self._handle_cloud_pool_blocking(prompt)` — no rotation argument.
- `_handle_cloud_pool_blocking` (`command_bridge.py:4472-4512`) **does**
  accept `pool_rotation: "CloudPoolRotation | None" = None` (used by the
  goal-loop path elsewhere, e.g.
  `test_handle_cloud_pool_blocking_rotates_start_provider`,
  `tests/test_command_bridge.py:3793`), but the plain-chat caller above never
  supplies one, so `rotated = chain` unchanged (line 4477) — **every plain
  chat turn always tries Gemini first.**
- `_stream_cloud_pool_chat` (`command_bridge.py:4514-4594`) doesn't even have
  a `pool_rotation` parameter — it hardcodes
  `for provider, model_name, build_fn, configured_fn in self._cloud_pool_chain():`
  (line 4518), always in the same fixed order, every single streamed turn.

**Consequence:** if Gemini is rate-limited or degraded for a stretch (a
documented real occurrence — see `WEB_CHAT_MULTIMODAL_PLAN.md` §1.3 "G1"
for the sibling precedent of a pool provider being silently unusable), every
single chat turn in the *same ongoing conversation* still pays the latency
of trying Gemini first and failing, before falling through to Mistral / Big
Pickle / local — even though the previous ten turns all already resolved to
Mistral. This wastes latency every turn and needlessly re-hits a
rate-limited provider's endpoint on a cadence proportional to chat volume,
not to any actual recovery signal.

### 1.3 Why this is NOT the same as `WEB_CHAT_MULTIMODAL_PLAN.md`'s rotation

`WEB_CHAT_MULTIMODAL_PLAN.md` (Status: Implemented — under acceptance
review) already covers `CloudPoolRotation` for splitting text vs. vision
pools and rotating *within* a request so multiple sub-calls in one turn
don't hammer one provider (WP-2.7: "one `CloudPoolRotation` instance per
stream request for the vision pool"). That is orthogonal to this plan:

| | `WEB_CHAT_MULTIMODAL_PLAN.md` rotation | This plan's sticky pin |
|---|---|---|
| Scope | within one request/turn | across many turns of one conversation |
| Goal | spread *multiple LLM calls in one turn* across providers | avoid re-trying a known-bad provider every turn |
| Mechanism | `CloudPoolRotation.rotate()` — cursor advances every call, no memory of success/failure | pin = last provider that *actually answered*; only changes on failure |
| State lifetime | one instance per request, discarded after | persists in `CommandBridge` for the conversation's lifetime |

Both are legitimate, additive, and non-conflicting — `CloudPoolRotation` is
not modified by this plan.

---

## 2. Design

### 2.1 Conversation identity (already exists, reused as-is)

`_conversation_key` (`command_bridge.py:1216-1218`):

```python
@staticmethod
def _conversation_key(req: WebCommandRequest) -> str:
    return req.conversation_id or req.session_id or "_default"
```

Already used to key per-conversation state elsewhere on `CommandBridge` —
precedent for the storage pattern this plan reuses:

- `self._live_notifiers: dict[str, Callable[[str], None]]` (`command_bridge.py:678`)
- `self._goal_continuations: dict[str, dict]` + `self._goal_cont_lock = threading.Lock()`
  (`command_bridge.py:690-691`), read/written under the lock at multiple
  sites (e.g. lines 1345, 1456, 1654, 1735, 1772, 1852).

`WebCommandRequest.conversation_id` / `.session_id` are plain optional
string fields (`command_bridge_models.py:142-143`), already parsed from the
incoming request dict (`command_bridge_models.py:299-300`), so no wire
format change is needed — `req` already carries everything required at
every call site this plan touches.

### 2.2 New state on `CommandBridge`

Add next to the `_goal_continuations` declaration (`command_bridge.py:690-691`):

```python
self._chat_pool_pins: dict[str, str] = {}
self._chat_pool_pins_lock = threading.Lock()
```

One entry per conversation key, value = the `provider` label (`"gemini"` /
`"mistral"` / `"opencode"`, matching the labels already used in
`_cloud_pool_chain()`'s tuples, `command_bridge.py:4398-4419`). A personal,
single-user bot accumulates at most a handful of live conversation keys at
once; this dict is never proactively evicted in this plan (see §7 open
question — acceptable low-risk simplification, same as the unbounded-but-tiny
`_goal_continuations` dict it sits next to).

### 2.3 New helper: pin-to-front chain reorder

Mirrors musubi's `nextVisionProviderOrder(pinned?)` (§1.1) exactly, adapted
to this repo's `(provider, model, build_fn, configured_fn)` tuple shape.
Add near `_walk_cloud_pool_chain` (`command_bridge.py:309-349`):

```python
def _pin_provider_chain(
    chain: list[tuple[str, str, object, object]],
    pinned: str | None,
) -> list[tuple[str, str, object, object]]:
    """Reorder ``chain`` so the entry whose provider label matches ``pinned``
    is first, preserving the relative order of everything else. No-op if
    ``pinned`` is None or not present in ``chain`` (e.g. the operator removed
    that provider from the pool since the pin was recorded)."""
    if pinned is None:
        return chain
    for i, entry in enumerate(chain):
        if entry[0] == pinned:
            return [entry, *chain[:i], *chain[i + 1:]]
    return chain
```

Pure function, module-level (not a method) — same testability rationale as
musubi's pure-function extractions (`sticky-worker.ts`, `task-scratchpad.ts`):
no `self`, no I/O, trivially unit-testable in isolation.

### 2.4 Thread `conversation_key` through the two entrypoints

`_generate_chat_response_blocking` (`command_bridge.py:4036-4038`) and
`_stream_chat_response` (`command_bridge.py:4076`) gain a trailing
keyword-only optional parameter — same "trailing-optional-param" convention
used throughout musubi's `selectAction`/`buildPrompt` extensions, so no
existing call site (there are none elsewhere for these two methods per
`grep`) breaks:

```python
def _generate_chat_response_blocking(
    self, prompt: str, chat_backend: str, *, conversation_key: str | None = None
) -> tuple[str, ModelMetadata]:
    ...
    if chat_backend == CHAT_BACKEND_CLOUD_POOL:
        return self._handle_cloud_pool_blocking(prompt, conversation_key=conversation_key)
    ...

def _stream_chat_response(
    self, prompt: str, chat_backend: str, *, conversation_key: str | None = None
) -> Iterator[dict]:
    ...
    if chat_backend == CHAT_BACKEND_CLOUD_POOL:
        yield from self._stream_cloud_pool_chat(prompt, conversation_key=conversation_key)
        return
    ...
```

Update the two call sites (both already have `req` in scope, confirmed by
`req.chat_backend` on the same lines):

- `command_bridge.py:943` →
  `self._generate_chat_response_blocking(prompt, req.chat_backend, conversation_key=self._conversation_key(req))`
- `command_bridge.py:1005` →
  `self._stream_chat_response(prompt, req.chat_backend, conversation_key=self._conversation_key(req))`

### 2.5 `_handle_cloud_pool_blocking` — read pin, walk, write pin

Current body, `command_bridge.py:4472-4512`:

```python
def _handle_cloud_pool_blocking(
    self, prompt: str, *, pool_rotation: "CloudPoolRotation | None" = None
) -> tuple[str, ModelMetadata]:
    """Try Gemini → Mistral → Big Pickle → local; return (text, metadata)."""
    chain = self._cloud_pool_chain()
    rotated = pool_rotation.rotate(chain) if pool_rotation is not None else chain
    text, provider, model_name, attempts = _walk_cloud_pool_chain(
        rotated, prompt, temperature=0.7
    )
    if text is not None:
        ...
```

New body — add `conversation_key`, apply the pin, record the outcome. In
practice the two callers of this method never populate both `pool_rotation`
and `conversation_key` (goal-loop supplies rotation only, plain chat supplies
the key only), but the code must still pick an explicit precedence: **a
matched pin wins and rotation is skipped for that call** (the sticky signal
is more specific to *this* conversation than a generic load-spreading
cursor). Do NOT write `pool_rotation.rotate(_pin_provider_chain(...))` —
rotating *after* pinning silently moves the pin off the front, i.e. rotation
would win, contradicting the intent. Branch explicitly:

```python
def _handle_cloud_pool_blocking(
    self,
    prompt: str,
    *,
    pool_rotation: "CloudPoolRotation | None" = None,
    conversation_key: str | None = None,
) -> tuple[str, ModelMetadata]:
    """Try Gemini → Mistral → Big Pickle → local; return (text, metadata)."""
    chain = self._cloud_pool_chain()
    pinned: str | None = None
    if conversation_key:
        with self._chat_pool_pins_lock:
            pinned = self._chat_pool_pins.get(conversation_key)
    if pinned is not None and any(entry[0] == pinned for entry in chain):
        rotated = _pin_provider_chain(chain, pinned)
    elif pool_rotation is not None:
        rotated = pool_rotation.rotate(chain)
    else:
        rotated = chain
    text, provider, model_name, attempts = _walk_cloud_pool_chain(
        rotated, prompt, temperature=0.7
    )
    if text is not None:
        if conversation_key:
            with self._chat_pool_pins_lock:
                self._chat_pool_pins[conversation_key] = provider
        fb = len(attempts) > 1
        first_provider, first_model = (
            (rotated[0][0], rotated[0][1]) if rotated else self._cloud_pool_preview()
        )
        return text, ModelMetadata(
            requested_provider=first_provider,
            requested_model=first_model,
            attempted_models=attempts,
            final_provider=provider,
            final_model=model_name,
            fallback_reason=None if not fb else f"Fell back from {attempts[0].provider}",
            fallback_occurred=fb,
            requested_tab=CHAT_BACKEND_CLOUD_POOL,
        )

    if provider_enabled(self.settings, LLM_PROVIDER_LOCAL):
        # local fallback unchanged from current code (lines 4497-4511);
        # deliberately does NOT update the pin -- "local" is the after-pool
        # fallback, not a pool member, same distinction the pool already
        # draws elsewhere (_cloud_pool_preview, _cloud_pool_chain).
        ...
    raise RuntimeError("雲端池目前沒有可用模型。")
```

Re-pinning happens on **every** successful call, not just when there was no
prior pin — so if the pinned provider itself fails this turn, whichever
provider the fallthrough lands on becomes the new pin next turn. This
mirrors musubi's runner re-pinning `state.currentTaskProvider = selection.provider`
after every step (not just the first) for the identical reason: a dead pin
must not get stuck.

### 2.6 `_stream_cloud_pool_chat` — same treatment for the streaming path

Current body: `command_bridge.py:4514-4594` (see §1.2 for the relevant
excerpt). Same three changes as §2.5:

1. Signature gains `*, conversation_key: str | None = None`.
2. Before `for provider, model_name, build_fn, configured_fn in
   self._cloud_pool_chain():` (line 4518), read the pin under the lock (same
   as §2.5 — reads and writes both hold `self._chat_pool_pins_lock`, matching
   the `_goal_continuations` precedent), compute
   `chain = _pin_provider_chain(self._cloud_pool_chain(), pinned)`, and
   iterate `chain`. This function has no `pool_rotation` parameter, so there
   is no precedence branch to worry about here. There are two other bare
   `self._cloud_pool_chain()` calls later in the same function feeding
   `first_provider`/`first_model` (`requested_provider`/`requested_model`
   metadata, lines ~4571-4572) — **replace those with `chain[0]` too**, so
   metadata reflects the actual walk order. Otherwise a pinned turn would
   report `requested_provider="gemini"`, `final_provider="mistral"`,
   `fallback_occurred=False` simultaneously — a misleading combination for
   any metadata consumer. The blocking path (§2.5) already gets this right
   for free because it reads `rotated[0]`.
3. On success (`attempts.append(ModelAttempt(provider, model_name, _MODEL_STATUS_OK))`,
   just before `yield stream_delta(text)`), add the same
   `if conversation_key: with self._chat_pool_pins_lock: self._chat_pool_pins[conversation_key] = provider`.

### 2.7 What does NOT change

- `CloudPoolRotation`, goal-loop's per-run rotation, and the chat-tool
  planner's rotation (§1.2 items 1–2) are untouched — different concern,
  different lifetime, proven pattern, do not conflate.
- `_cloud_pool_chain()` itself (`command_bridge.py:4398-4419`) — unchanged;
  `_pin_provider_chain` operates on its *output*, not its construction.
- The vision pool / `WEB_CHAT_MULTIMODAL_PLAN.md` work — orthogonal, no
  overlap (§1.3).
- `aka_no_claw_web` (frontend) — zero changes. `ModelMetadata.final_provider`
  already surfaces which provider answered each turn
  (`command_bridge_models.py:202+`); if the UI ever wants to show "still on
  Mistral" that data is already in the response, this plan doesn't need to
  add anything for it.
- Telegram-side chat — this whole `command_bridge.py` stack is the web
  bridge (`WebCommandRequest`); Telegram has its own separate path in
  `telegram_bot.py` and is not touched, same scoping note as
  `WEB_CHAT_MULTIMODAL_PLAN.md` §7.

---

## 3. Work packages

### WP-1 — pin storage + pure helper

1. Add `self._chat_pool_pins` / `self._chat_pool_pins_lock` next to
   `_goal_continuations` (`command_bridge.py:690-691`).
2. Add `_pin_provider_chain` as a module-level function near
   `_walk_cloud_pool_chain` (`command_bridge.py:309-349`), per §2.3.
3. Unit tests (new, in `tests/test_command_bridge.py` near the existing
   `_walk_cloud_pool_chain`-adjacent tests): pin moves matching entry to
   front, preserves the rest's relative order; `pinned=None` is a no-op;
   `pinned` not present in chain (provider disabled/removed since the pin
   was recorded) is a no-op, not an error.

### WP-2 — blocking chat path

1. Update `_handle_cloud_pool_blocking` per §2.5.
2. Update `_generate_chat_response_blocking` + its one call site
   (`command_bridge.py:943`) per §2.4.
3. Tests in `tests/test_command_bridge.py`, following the exact style of
   the existing `_FakeCloudClient` + `_tool_settings` + `parse_request` +
   `b.handle(req)` pattern (lines 3601-3712) and the existing
   `test_handle_cloud_pool_blocking_rotates_start_provider` (line 3793):
   - Two `b.handle(req)` calls with the **same** `conversation_id` in the
     request dict; first call's provider fails (`_FakeCloudClient(...,
     fail=True)`) and falls to a second provider; assert the **second**
     `b.handle(req)` call goes straight to the provider that won last time
     (i.e. does not re-attempt the first, failed provider — check
     `meta["attempted_models"]` has length 1 on the second call).
   - Two calls with **different** `conversation_id`s both hitting the same
     bridge instance: assert they do NOT share a pin (second conversation
     still tries Gemini first even though the first conversation pinned
     Mistral).
   - Pinned provider goes down on a later turn: assert fallthrough still
     works (existing fail-and-fall-through behavior unchanged) and the pin
     updates to the new winner.

### WP-3 — streaming chat path

1. Update `_stream_cloud_pool_chat` per §2.6.
2. Update `_stream_chat_response` + its one call site
   (`command_bridge.py:1005`) per §2.4.
3. Tests mirroring `test_cloud_pool_stream_gemini_success` /
   `test_cloud_pool_stream_all_fail_fallback_local`
   (`tests/test_command_bridge.py:3714-3729+`), using `b.stream(req,
   "test-rid")` twice with the same `conversation_id`, asserting the second
   stream's `done` event metadata shows the pinned provider with no
   fallback attempts.

### WP-4 — regression check

Run the existing goal-loop / chat-tool-planner rotation tests unchanged
(`test_generate_cloud_pool_chat_tool_plan_rotates_start_provider`,
`test_handle_cloud_pool_blocking_rotates_start_provider`,
`test_goal_llm_transform_client_cloud_pool_uses_rotation`, all in
`tests/test_command_bridge.py`) to confirm `conversation_key=None` (their
default, since none of these callers pass it) leaves `_pin_provider_chain`
a no-op and behavior byte-identical to today.

### WP-5 (optional, decide before implementing — see §7) — settings toggle

If a kill-switch is wanted: add `sticky_cloud_pool: bool` to
`ChatLlmPoolSettings` (`llm_pool_settings.py:159-164`), threaded through
`normalize_chat_llm_pool_settings` (`llm_pool_settings.py:222-260ish`) and
`default_chat_llm_pool_settings` (`llm_pool_settings.py:299-...`) the same
tolerant-default way `vision_pool`/`vision_providers` were added
(`WEB_CHAT_MULTIMODAL_PLAN.md` WP-1.3, same file). `_handle_cloud_pool_blocking`
/ `_stream_cloud_pool_chat` would read
`chat_llm_pool_settings(self.settings).sticky_cloud_pool` (or equivalent
accessor) to decide whether to consult `self._chat_pool_pins` at all. This
is more machinery than a single-user bot strictly needs; §7 asks whether to
build it.

---

## 4. Acceptance criteria

1. Same `conversation_id`, consecutive `cloud_pool` chat turns: once a
   provider answers, the next turn's `model_metadata.attempted_models` has
   length 1 (no re-attempt of anything earlier in the chain) as long as that
   provider keeps succeeding.
2. If the pinned provider fails on some later turn, that turn's
   `attempted_models` shows the failure + the fallthrough winner (existing
   behavior, unchanged), and the pin updates to the new winner for the turn
   after.
3. Two different `conversation_id`s never share a pin.
4. Goal-loop and chat-tool-planner rotation behavior is unchanged (WP-4).
5. No `aka_no_claw_web` changes required or made.
6. All new code is plain mechanical provider-ordering — no keyword lists,
   no domain classification — consistent with Rule G (no hardcode).

## 5. Out of scope

- Telegram-side chat (separate code path, not touched — see §2.7).
- `vision_pool` / `WEB_CHAT_MULTIMODAL_PLAN.md`'s rotation-within-one-turn
  mechanism (orthogonal, unmodified — see §1.3).
- Persisting pins across a bridge restart (in-memory only, same lifetime as
  `_goal_continuations`/`_live_notifiers`; acceptable, a restart is rare and
  the first post-restart turn just re-discovers the right provider in one
  extra hop).
- Any UI surfacing of "which provider is currently pinned" — the data is
  already in `ModelMetadata`; a UI affordance is a separate, later decision.

## 6. Testing / verification plan

- `pytest` — single-invocation style per this repo's collab rules (no
  heredoc, no env-var prefix): `.venv/bin/python -m pytest tests/test_command_bridge.py -k "pool_pin or sticky"`
  (adjust `-k` once test names are finalized) plus a full
  `.venv/bin/python -m pytest` before calling this done.
- Live spot-check (optional, not required for correctness): open two
  separate browser tabs/sessions against the web console (different
  `conversation_id`s), force one provider to fail in one tab (e.g. via a
  temporarily-wrong key in a throwaway settings copy, never the real
  `.env`), confirm the other tab's pin is unaffected.
- Do not touch the manually-run bridge on port 8781 for any live check —
  verify on a throwaway port (8799) per the standing collab rule.

## 7. Open questions (resolve before or during implementation)

1. **Default on or behind a flag?** musubi gated every new behavior behind
   an env flag defaulting off, because Case C had a literal-reproducibility
   runbook constraint that doesn't exist here. This is a personal
   single-user bot with no such constraint, and the change is a strict
   latency/quota improvement with no plausible downside once tested — leans
   toward shipping WP-1–WP-4 unconditionally and treating WP-5 (settings
   toggle) as unnecessary unless a reason turns up during implementation.
   Confirm with the user before writing WP-5.
2. **Pin eviction** — none implemented in this plan (§5). If the live
   `_chat_pool_pins` dict ever needs bounding (it won't, realistically, for
   a single-user bot), the natural hook is wherever `_goal_continuations`
   entries already get cleaned up.

## 8. Progress log

- 2026-07-07: plan written after confirming (via direct code reads, not
  assumption) that `aka_no_claw_web`'s `cloud_pool` chat backend has the
  identical structural gap musubi's issue #11 Case C P1 fix ("sticky
  provider") was built to close. No code changed yet in this repo. Origin
  conversation: musubi-for-tenkyoku's issue-11 Case C structural
  improvements session, same day.
- 2026-07-07 (later, self-review pass): re-audited the musubi origin
  implementation (`nextVisionProviderOrder` splice/unshift with idx -1/0
  no-ops, `ACTION_SELECTOR_FORCE_PROVIDER` priority, runner re-pin every
  step at `phase6-runner.ts:1131`, pin cleared at task boundaries at
  `:774`/`:1013`; `tests/sticky-selection.test.ts` 9/9 passing) — origin is
  sound. Two fixes applied to this plan as a result:
  1. §2.5 had a pin-vs-rotation precedence bug: the draft applied
     `pool_rotation.rotate()` *after* pinning, which rotates the pin off the
     front (rotation would silently win, contradicting the stated
     precedence). Rewritten as an explicit branch: matched pin skips
     rotation for that call.
  2. §2.6 originally kept the streaming path's bare
     `self._cloud_pool_chain()[0]` metadata reads, which on pinned turns
     would emit the misleading `requested_provider=gemini` +
     `final_provider=mistral` + `fallback_occurred=False` combination; now
     those reads switch to `chain[0]` (safe: they execute inside the loop's
     success branch, so the chain is non-empty). Pin reads now also hold
     `self._chat_pool_pins_lock`, matching writes and the
     `_goal_continuations` locking precedent.
  Known origin-side quirk, deliberately not ported: musubi re-pins even when
  the text-only safety net (not the vision pool) served a step, leaving a
  pin label that isn't in the vision pool — harmless there because a
  non-matching pin is a no-op and self-heals next step. This plan has no
  equivalent split-pool ambiguity: only actual `_cloud_pool_chain()` members
  ever get pinned, and the local fallback deliberately never writes the pin
  (§2.5 code comment).
- 2026-07-07 (live E2E acceptance pass, post-restart): full pytest green
  (2339 passed / 7 skipped / 1 unrelated pre-existing failure), and the
  already-implemented WP-1–WP-4 code matched the corrected plan exactly.
  But live HTTP E2E against the freshly-restarted bridge (`POST
  /api/command`, `chat_backend=cloud_pool`, same `conversation_id` across
  two turns) exposed a real gap neither review nor unit tests caught:
  **ordinary conversational turns never touch the pin-aware code at all.**
  `_handle_chat_blocking`/`_stream_chat` answer the common
  `CHAT_TOOL_NO_TOOL` case straight from the hidden chat-tool-planner call
  (`_select_chat_tool_plan` → `_generate_chat_tool_plan_with_chat_backend`
  → `_generate_cloud_pool_chat_tool_plan`) — a path with zero pin awareness
  before this fix. `_handle_cloud_pool_blocking`/`_stream_cloud_pool_chat`
  (the code WP-1–WP-4 actually patched) only run on the rare "tool router
  returned no plan" fallback branch. Surfaced naturally: Gemini returned a
  genuine `HTTP 429 RESOURCE_EXHAUSTED` mid-test, giving a real (not
  simulated) failover to observe, and turn 2 did not stick to whichever
  provider had actually succeeded on turn 1.
  Fix: threaded `conversation_key` through
  `_generate_cloud_pool_chat_tool_plan` (same pin-then-rotate-else branch
  as §2.5), `_generate_chat_tool_plan_with_chat_backend`, and
  `_select_chat_tool_plan`'s one call site (no signature change needed
  there — `req` was already in scope), which transparently covers both the
  blocking and streaming paths. Deliberately did NOT add
  `conversation_key` to the other two callers of
  `_generate_chat_tool_plan_with_chat_backend` (`_goal_planner_client`,
  `_generate_chat_tool_satisfaction_text`) — those are goal-loop-run-scoped
  by design and must stay unpinned to avoid conflating run scope with
  conversation scope.
  Side effect: 9 existing tests broke, because the planner call and the
  fallback call now share the same `_chat_pool_pins` dict, so the
  planner's own (always-non-JSON-in-tests) cloud_pool walk started writing
  the pin before the fallback call each test meant to isolate even ran.
  This is correct new intra-turn behavior (reusing a provider the planner
  just proved works avoids a guaranteed-fail retry in the same turn), not
  a bug, so the fix was to restore each test's intended isolation via
  `monkeypatch.setattr(b, "_select_chat_tool_plan", lambda req,
  observation=None: (None, None))` (8 tests), plus one signature fix
  (`_planner(_backend, prompt, **_kwargs)`) for a test whose fake didn't
  accept the new kwarg. Full `tests/test_command_bridge.py` green (237
  passed) after the fix cascade. Live bridge (port 8781) was still running
  the pre-fix code as of this entry — needs another `/restartall` before
  the fix is actually live for a repeat E2E confirmation.
- 2026-07-07 (post-restart full acceptance): user restarted the stack;
  verified live via `tmux -L openclaw_codex list-panes` + cross-referenced
  real child PIDs + `lsof ESTABLISHED` to Telegram's IP (poller genuinely
  polling, not just process-alive). Ran real HTTP E2E against the fixed
  bridge on 8781 (no fakes, real provider keys):
  1. Two-turn ordinary chat, same `conversation_id`, `chat_backend=cloud_pool`
     — both turns answered directly through the now-fixed planner path
     (`model_metadata` present in the response for the `CHAT_TOOL_NO_TOOL`
     case, confirming `_select_chat_tool_plan` → …→
     `_generate_cloud_pool_chat_tool_plan` runs and returns metadata
     end-to-end). Mistral succeeded both turns (head of chain), so this
     confirms the wiring executes cleanly live but does NOT by itself prove
     the pin skips an already-failed provider — no real provider failure
     occurred during this pass to force that branch.
  2. Turn 2's input ("她最近有出新單曲嗎") was independently upgraded by the
     existing (unrelated-to-this-fix) `_maybe_upgrade_tool_result_to_goal_loop`
     path into a real 3-step goal-loop run (`llm_transform` ×2 →
     `/saynow`), judged `satisfied=True`, and spoke the result aloud via
     AivisSpeech — confirmed live in `logs/openclaw.log.2`. This is the
     "多步驟計劃案例" the user asked to reconfirm; it ran clean post-restart
     with no errors in the log window.
  3. Sent `建立工作流: 播放最愛音樂清單 然後 開燈` (the exact phrasing that
     previously succeeded via Telegram NL routing) through the chat
     `cloud_pool` endpoint directly — `CHAT_TOOL_CREATE_WORKFLOW` fired,
     Mistral generated a valid draft (`wf-play-favorites-and-turn-on-light`,
     2 steps: `/music playbest`, `/ir send ceiling_light power`), pending
     save/review — confirming the bundled workflow-save feature (reviewed
     but out-of-scope for the sticky-pin fix itself) still works
     unmodified. This is the "建立工作流案例" the user asked to reconfirm.
  4. Streaming path (`POST /api/command/stream`) also verified end-to-end:
     clean `start` → `delta` → `done` sequence with `model_metadata`
     present on `done`, same as the blocking path.
  5. Full suite: `pytest -q` → 2339 passed / 7 skipped / 1 failed — the 1
     failure is `test_fix_command.py::test_handler_repair_then_callback_apply`
     (an attempt-file count assertion, `assert 2 == 1`), same pre-existing
     unrelated failure noted earlier in this log, not a regression.
  Known residual gap: no unit test yet directly exercises the new
  `conversation_key` threading through the *planner* path
  (`_generate_cloud_pool_chat_tool_plan`) with a forced provider failure —
  the 8 fixed pin tests all still bypass `_select_chat_tool_plan` by design
  (they isolate the fallback path, per the entry above), and this pass's
  live E2E only exercised the trivial all-succeeds case. If a future
  session has spare quota, add a dedicated test using a JSON-emitting fake
  planner client (so `_select_chat_tool_plan` returns a real
  `CHAT_TOOL_NO_TOOL` plan instead of falling back) with one provider
  forced to fail, asserting turn 2 skips straight to the pinned provider —
  this is the one scenario this fix was written for that is still
  unverified by an automated regression test.
