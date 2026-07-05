# Web Chat Multimodal Plan (issue #71)

Last reviewed: 2026-07-05
Status: Implemented — under acceptance review (static review + E2E)
Owner area: command_bridge / vision_pool
Created: 2026-07-05
Issue: https://github.com/jojojen/aka_no_claw/issues/71

Goal: web UI chat gains image upload; the chat flow cooperates between a
**text-only cloud pool** and a **multimodal (vision) cloud pool**, rotating from
the appropriate pool per request type; the planner can call vision as a tool
(e.g. Mercari listing → fetch product images → condition/discount assessment);
the top-right model settings UI exposes the vision pool. If the cloud
multimodal path beats the current local OCR+text two-step, image translation
is replaced too (§WP-7).

This doc is written to be implementable by a cold agent (including a free
cloud model): every touch point cites file + line as of 2026-07-05.

---

## 1. References

### 1.1 External research — why "vision-as-a-tool", not mixed-modality history

The chosen architecture keeps conversation history **text-only**; a vision
model is invoked as a *tool* whose textual observation flows back into the
text model's context. This is a validated pattern, not an improvisation:

- **MM-ReAct** (Yang et al., arXiv:2303.11381,
  https://arxiv.org/abs/2303.11381): a *text-only* LLM acts as orchestrator;
  vision experts are tools; their textual outputs are injected back into the
  LLM's reasoning context. Training-free, exactly our topology.
- **HAMMR** (arXiv:2404.05465, https://arxiv.org/abs/2404.05465):
  hierarchical multimodal ReAct agents — confirms the tool-call pattern
  scales to multi-step VQA without multimodal history.
- Survey/index: https://github.com/jun0wanan/awesome-large-multimodal-agents
  (§"LMM as tool-user" family).
- **In-house proof**: musubi-for-tenkyoku
  `packages/server/src/phase6d/action-selector.ts` "Route A" — text-only pool
  is primary, vision escalation only on demand, vision output returned **as
  text**. Ran through the Phase 6 official-evaluator experiments without
  mixed-history bugs.

Why not images-in-history: every follow-up turn would re-bill vision tokens
on the whole history, history serialization (`ChatTurn` is `role`+`content`
str, `command_bridge_models.py:125-128`) would need a breaking schema change,
and only some pool providers accept images — a text-only history keeps every
provider eligible for every non-image turn.

### 1.2 Provider capability (web-verified 2026-07-05; MUST re-verify live, §WP-3)

- **Gemini free tier**: all modalities included; a text+image request counts
  as one request toward RPM/RPD. Docs: https://ai.google.dev/gemini-api/docs/pricing ,
  https://ai.google.dev/gemini-api/docs/rate-limits ,
  https://ai.google.dev/gemini-api/docs/models
- **Mistral free API tier**: vision-capable models available (pixtral family;
  Large-3 supports vision). Docs: https://docs.mistral.ai/capabilities/vision ,
  https://docs.mistral.ai/models/overview
- **OpenCode zen free models** (big-pickle etc.): mostly NO vision — excluded
  from the vision pool unless the live probe proves otherwise.
  Docs: https://opencode.ai/docs/zen/
- **Local**: qwen2.5vl via Ollama — already wired (`image_translate.py`),
  becomes the vision-pool fallback entry.

### 1.3 In-house lessons imported from musubi (binding guardrails)

- **G1 — live provider certification**: musubi's pool once had Gemini
  "configured but silently unwired" despite valid `.env` keys. Every vision
  pool member must pass a *real image request* probe before being certified
  (§WP-3). Web docs alone don't count.
- **G2 — bounded waits**: musubi Case B run2 froze 55 min because a 429
  `Retry-After` sleep was unclamped and a response-body read outlived its
  abort timer (fixed in musubi `packages/server/src/phase6d/llm-client.ts`,
  `callOpenAICompat`: clamp wait to deadline via `Math.min`, keep the abort
  timer alive until after `resp.json()`). Any new vision HTTP client here
  must (a) clamp retry waits, (b) keep body reads under the timeout.
- **G3 — no hardcode (Rule G)**: which images matter, whether damage is
  visible, how much to discount — all LLM judgment. No keyword lists, no
  site-specific CSS selectors for "the damage photo". Structural parsing
  (og:image, `<img>` tags) is allowed; open-world judgment is not.
- **G4 — registry single source of truth**: the vision chat-tool's
  name/usage/purpose/query-hint live on its `RegisteredCommand` row, never a
  parallel dict in the bridge (matches how `/research` etc. are surfaced,
  `command_bridge.py:1992-2012`).

---

## 2. Current-state map (all verified 2026-07-05)

### Backend — `aka_no_claw/src/openclaw_adapter/`

| Concern | Where | Facts |
|---|---|---|
| Attachment wire format | `command_bridge_models.py:89-122` | `Attachment{type, filename, content_type, data: bytes}`; frontend sends `data_base64`, `from_dict` base64-decodes; bytes carried in-process only |
| Request + image flag | `command_bridge_models.py:131-147` | `WebCommandRequest.has_image_attachment` ⇒ any attachment with `type == "image"` |
| Chat history | `command_bridge_models.py:125-128` | `ChatTurn{role, content}` — text-only today; **keep it that way** |
| Pool settings | `llm_pool_settings.py:130-144` | `ChatLlmPoolSettings{default_chat_provider, cloud_pool, providers}`; persisted to `config/llm_pool.json` (`_config_path`, :439-442) |
| Pool normalize/defaults | `llm_pool_settings.py:186-247` | tolerant of missing keys → back-compat for new fields is free |
| Settings payload for UI | `llm_pool_settings.py:250-269` | `chat_llm_pool_payload()` → labels, enabled, model, configured, `model_options` |
| Rotation | `llm_pool_settings.py:99-121` | `CloudPoolRotation.rotate(items)` — generic over `items`, reusable as-is for a second pool |
| Text pool walk | `command_bridge.py:279-319` | `_walk_cloud_pool_chain(chain, prompt, temperature)`; chain entries `(provider, model, build_fn, configured_fn)`; `build_fn(model)` for gemini else `build_fn()` |
| Text pool chain | `command_bridge.py:3840-3862` | `_cloud_pool_chain()` builds entries from `enabled_cloud_pool_providers()` |
| Client builders | `command_bridge.py:3666-3694` | `_build_cloud_chat_client` (OpenCode zen/v1), `_build_mistral_chat_client`, `_build_gemini_chat_client(model)` |
| Chat entry | `command_bridge.py:826-860` | `_stream_chat`: goal-control fast paths → planner (`_stream_chat_tool_plan`) → `__goal__` / tool / `__no_tool__` / degraded plain chat |
| Planner prompt | `command_bridge.py:342-362` (template), `1952-1982` (builder) | tool lines from registry via `_registered_chat_tool_prompt_line` (:1992-2012) |
| Planner backends | `command_bridge.py:2048-2087` | per-backend planner generation; cloud-pool variant :2121-2146 |
| Tool dispatch | `command_bridge.py:2148-2177` | `_run_chat_tool` `policy_map`; most tools run `_exec_registered_command_chat_tool` |
| Translation routing | `command_bridge.py:4090-4092` | `submode == image_translation or has_image_attachment` → image translation |
| Local vision | `image_translate.py:64-125` | `encode_image_for_vision` (downscale longest side→1280, JPEG q90), `call_ollama_vision` (Ollama `/api/generate` + `images:[b64]`) |
| Two-step OCR+translate | `image_translate.py:183-259` | qwen2.5vl OCR → qwen3:14b JSON translate; built only from local settings |
| Registry rows | `telegram_bot.py:1573-1824` | `RegisteredCommand` map (`/quiz`, `/zh`, `/new`, …) — where the vision tool row goes |

### Frontend — `aka_no_claw_web/frontend/src/`

| Concern | Where | Facts |
|---|---|---|
| Attachment button gating | `components/InputBar.tsx:54-56` | `{mode === "translation" && (<AttachmentButton onSelect={onSelectImage} disabled={generating} />)}` |
| File→base64 | `App.tsx:70-87` | `fileToBase64` strips the `data:` URL prefix |
| Image upload flow | `App.tsx:909-951` | `onSelectImage` guard `mode !== "translation"` (:911); builds `mode:"translation", submode:"image_translation"` request with one `attachments[]` entry (:933-946); runs blocking |
| Chat request builder | `App.tsx:293-322` | `buildRequest` always sends `attachments: []` for chat |
| Settings modal | `components/ChatSettingsModal.tsx` | pool reorder (:29-46), provider cards + model radios (:94-160) driven by `providers` / `model_options` / `cloud_pool` payload keys |

### Settings server

`command_bridge_server.py` chat-settings GET/POST endpoints pass the payload
through `normalize_chat_llm_pool_settings`/`save_chat_llm_pool_settings` —
new fields flow through once normalize knows them (verify exact lines when
implementing; the Explore map put them near :246-265).

---

## 3. Architecture

```
user turn (text [+ image attachment])
        │
        ├─ image attached? ──► [Vision observe step]                (WP-5a)
        │                      save bytes → temp file → encode_image_for_vision
        │                      → vision pool walk (WP-2) with OBSERVE prompt
        │                      → textual observation ("圖片觀察：…")
        │                      → prepended to planner context; also emitted
        │                        into the visible transcript, so history
        │                        stays text-only *and* self-contained
        │
        ▼
  chat tool planner (text pool / selected backend, unchanged path)
        │           planner may pick the vision tool for URL images (WP-5b)
        ▼
  __no_tool__ answer │ /search │ /research │ … │ /visionlook (new)
                                                     │
                                     query = instruction (+ URL when the
                                     image lives on a page)
                                     executor: acquire images (WP-6)
                                     → vision pool walk → text result
                                     → synthesized back like other tools
```

Two pools, one rotation class:

- **text pool** — today's `cloud_pool` (unchanged).
- **vision pool** — new `vision_pool` order + `vision_providers` configs;
  its own `CloudPoolRotation` instance; `local` (qwen2.5vl) is a legitimate
  pool member here (unlike the text pool where local is the after-pool
  fallback), because it is the only no-quota vision engine.

---

## 4. Config schema — `config/llm_pool.json` (additive, back-compat)

```json
{
  "default_chat_provider": "cloud_pool",
  "cloud_pool": ["gemini", "mistral", "big_pickle"],
  "providers": { "…existing, unchanged…": {} },

  "vision_pool": ["gemini", "mistral", "local"],
  "vision_providers": {
    "gemini":  {"enabled": true,  "model": "gemini-2.5-flash"},
    "mistral": {"enabled": true,  "model": "pixtral-12b-latest"},
    "local":   {"enabled": true,  "model": "qwen2.5vl"}
  }
}
```

Missing `vision_*` keys → defaults (same tolerant-normalize pattern as
`llm_pool_settings.py:186-227`). `big_pickle` is **excluded** from the vision
pool (decision 2026-07-05): live probe confirmed no `big-pickle-vision` model
exists on the zen API (`/models` list + direct request → "Model not
supported") and free zen models have no image support. Vision pool members
are gemini / mistral / local only.

Model names above are placeholders until WP-3 certifies exact IDs.

---

## 5. Work packages

### WP-1 — vision pool settings (`llm_pool_settings.py`)

1. Extend `ChatLlmPoolSettings` (:130-144) with `vision_pool: tuple[str, ...]`
   and `vision_providers: dict[str, ProviderSettings]`; extend `to_dict`.
2. New module constants mirroring :29-39:
   `_DEFAULT_VISION_POOL = ("gemini", "mistral", "local")`,
   `_ALL_VISION_PROVIDERS = ("gemini", "mistral", "local")`.
3. `normalize_chat_llm_pool_settings` (:186-227): normalize the two new keys
   with the same shape of logic; generalize `_normalize_cloud_pool`
   (:395-411) to take the allowed-provider set as a parameter (it currently
   pins `_DEFAULT_CLOUD_POOL`).
4. `default_chat_llm_pool_settings` (:230-247): vision defaults —
   gemini model from `openclaw_gemini_primary_model` (vision-capable per
   §1.2), mistral vision model constant (WP-3 decides the exact ID), local
   from `openclaw_local_vision_model` (same source
   `build_image_ocr_translate_renderer_from_settings` reads,
   `image_translate.py:221-223`).
5. New accessors mirroring :297-303:
   `vision_pool_order(settings)`, `enabled_vision_pool_providers(settings)`,
   `resolve_vision_provider_model(settings, provider)`.
6. `chat_llm_pool_payload` (:250-269): add `vision_pool`,
   `vision_providers` (with `label`/`enabled`/`model`/`configured`) and
   `vision_model_options` keys. Vision recommended-model tuples live here
   next to `_GEMINI_RECOMMENDED_MODELS` (:74-92); fill from WP-3 results.
7. Tests (extend the existing llm_pool_settings pytest module): defaults
   when keys absent; unknown provider dropped; round-trip save/load; a
   legacy file without vision keys loads clean.

### WP-2 — vision clients + pool walk (new file `vision_pool.py`)

New module so `command_bridge.py` (already ~4k lines) doesn't grow the HTTP
plumbing. Contents:

1. **Client protocol**: `generate(prompt: str, images_b64: list[str], *,
   temperature: float) -> str`.
2. **`GeminiVisionClient`** — `POST
   https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`
   with key header, body:

   ```json
   {"contents": [{"parts": [
       {"text": "<prompt>"},
       {"inline_data": {"mime_type": "image/jpeg", "data": "<b64>"}}
   ]}], "generationConfig": {"temperature": 0.2}}
   ```

   Reference implementations to copy from: the existing Gemini *text* client
   this repo builds in `_build_gemini_chat_client`
   (`command_bridge.py:3691-…`) for auth/error/status conventions
   (`_GeminiRequestError`), and musubi
   `packages/server/src/phase6d/llm-client.ts` `callGemini`, which already
   sends `inline_data` image parts — it is the working template for the
   payload shape. Response text extraction: reuse `_extract_gemini_text`
   (`command_bridge.py:322-335`).
3. **`MistralVisionClient`** — `POST https://api.mistral.ai/v1/chat/completions`,
   OpenAI-style content-array message:

   ```json
   {"model": "<model>", "messages": [{"role": "user", "content": [
       {"type": "text", "text": "<prompt>"},
       {"type": "image_url", "image_url": "data:image/jpeg;base64,<b64>"}
   ]}], "temperature": 0.2}
   ```

   Per https://docs.mistral.ai/capabilities/vision . Follow
   `MistralTextClient` (in `dynamic_tools`, used at
   `command_bridge.py:3681-3689`) for auth and the 4-RPM courtesy pacing the
   repo already applies to Mistral.
4. **`LocalVisionClient`** — thin wrapper over `call_ollama_vision`
   (`image_translate.py:91-125`); multi-image = `images: [b64, b64, …]`
   (Ollama API accepts a list already).
5. **`walk_vision_pool_chain(chain, prompt, images_b64, *, temperature)`** —
   mirror of `_walk_cloud_pool_chain` (`command_bridge.py:279-319`): same
   `(provider, model, build_fn, configured_fn)` entries, same
   `ModelAttempt` bookkeeping, first success wins, returns
   `(text, provider, model, attempts)`.
6. **G2 compliance** (mandatory): every client — clamp any `Retry-After`
   wait to a per-call deadline (`min(retry_after, deadline - now)`), and
   ensure the read timeout covers the *body read*, not just connect
   (stdlib `urlopen(timeout=…)` as used in `image_translate.py:115` already
   applies to reads; keep that pattern).
7. Bridge side (`command_bridge.py`): `_vision_pool_chain()` mirroring
   `_cloud_pool_chain` (:3840-3862) but reading
   `enabled_vision_pool_providers` + `resolve_vision_provider_model`; one
   `CloudPoolRotation` instance per stream request for the vision pool
   (create it next to the existing text-pool rotation, cf. :1650).
8. Image prep: always route bytes through `encode_image_for_vision`
   (`image_translate.py:68-88`) — the 1280px downscale exists because
   full-res photos blow up vision token counts; cloud quotas benefit the
   same way local context did. Cap images per call at 3.
9. Unit tests with fake clients: order, skip-not-configured, first-success,
   all-fail → `(None, …)`.

### WP-3 — live certification probe (gate for everything below)

Per G1 and the user's directive: web check ≠ certified. Script
`scripts/probe_vision_providers.py`:

1. Build a deterministic probe image *in code* (Pillow: 200×200, half red /
   half blue, the digit "7" drawn on it) — no fixture file, no network.
2. For each candidate `(provider, model)` from §1.2 (gemini primary+flash,
   mistral pixtral + large-3, local qwen2.5vl): send the probe with prompt
   "describe the colors and the digit in this image, one line."
   (big_pickle dropped 2026-07-05 — no vision variant exists, see §4.)
3. Certification = HTTP 200 AND the reply demonstrably reflects the image
   (mentions the digit or both colors — checked by eye, printed to stdout;
   NOT an automated keyword gate, per Rule G this is a human/agent judgment
   step in a run log, not runtime logic).
4. Record the certified matrix (provider, model, latency, notes) in §8 of
   this doc; only certified pairs enter `_DEFAULT_VISION_POOL` and the
   recommended-model tuples (WP-1.6).
5. Re-run whenever a provider/model is added — same rule as musubi's
   provider verification.

Run shape: plain `.venv/bin/python scripts/probe_vision_providers.py`
(no heredoc, no env-var prefix — collab rules).

### WP-4 — frontend: chat upload + settings UI (`aka_no_claw_web`)

1. `components/InputBar.tsx:54-56`: widen gating to
   `{(mode === "translation" || mode === "chat") && (…)}`.
2. `App.tsx`: today `onSelectImage` (:909-951) is translation-specific and
   fires immediately with an empty `input`. For chat we want image+question
   in one turn, so switch to a **staged attachment**: selecting a file in
   chat mode stores `{file, previewUrl}` in state and shows a thumbnail chip
   above the input (with ✕ to clear); `onSend` (:838-846 area) then calls
   `fileToBase64` (:73) and includes

   ```ts
   attachments: [{ type: "image", filename: file.name,
                   content_type: file.type, data_base64 }]
   ```

   in the chat request (`buildRequest` :293-322 gains an optional
   attachments argument; other modes keep `[]`). Translation mode keeps its
   existing immediate-fire behavior.
3. `ChatSettingsModal.tsx`: add a「多模態池」section — same reorder list
   (:94-115) and provider cards (:127-160) rendered from the new
   `vision_pool` / `vision_providers` / `vision_model_options` payload keys;
   `updateProvider` (:29-35) generalized or duplicated as
   `updateVisionProvider`. Save path posts the whole draft; backend
   normalize (WP-1.3) does the rest.
4. Types: extend the `ChatSettings` shape in `lib/commandClient.ts`
   (`getChatSettings`/`saveChatSettings`) with the three new keys.
5. Verification: run the dev server, upload a real photo in chat, watch the
   vision observation and answer stream in; regression-check translation
   upload and text-only chat (BROWSER_UI_VALIDATION_PLAYBOOK.md).

### WP-5 — bridge orchestration (vision-as-a-tool)

**5a. Current-turn attachment → observe step.** In `_stream_chat`
(`command_bridge.py:826-860`), after the goal-control fast paths and before
`_build_chat_tool_plan_prompt`:

1. If `req.has_image_attachment`: write `attachment.data` to a
   `tempfile.NamedTemporaryFile(suffix=".jpg")`, encode via
   `encode_image_for_vision`, run the vision pool walk with the OBSERVE
   prompt (below), streaming heartbeats via the same worker-thread pattern
   as `_stream_chat_tool_plan` (:1921-1950).
2. OBSERVE prompt (generic, no domain wording — G3): user's message text +
   「請客觀描述這張圖片中可見的內容與狀態，包括任何可見的瑕疵、損傷、文字。
   用繁體中文、條列、不要臆測圖片外的資訊。」
3. Emit the observation to the transcript as a visible delta
   (「🔍 圖片觀察：…」) **and** append it to the planner context as an extra
   turn (`對話紀錄` block built at :1952-1958) so the tool decision and the
   final answer both see it. Because it is emitted as message text, it rides
   the existing text-only history (#44) into future turns for free.
4. Vision pool total failure → say so in one line and continue text-only
   (mirror the degrade message pattern at :856-860); never hard-fail chat.

**5b. Planner-invoked vision tool (for URL images / follow-ups).**

1. Register a `RegisteredCommand` row (G4) next to its peers in
   `telegram_bot.py` (`_COMMAND_METADATA` map, rows at :1573-1824), e.g.
   `/visionlook`, with `usage`, `chat_tool_purpose`（何時需要實際查看圖片/
   商品照片來回答）and `chat_tool_query_hint`（query = 要查看的目標與想
   確認的重點；若圖片在網頁上，附上網址）. The planner prompt then picks it
   up automatically via `_registered_chat_tool_prompt_line`
   (`command_bridge.py:1992-2012`) — zero prompt-side hardcoding.
2. Add `CHAT_TOOL_VISION` to the planner tool tuple (:1965-1972) and a
   `(_VISION_TOOL_POLICY, self._exec_vision_chat_tool)` entry in
   `policy_map` (:2150-2163).
3. `_exec_vision_chat_tool`: resolve image sources — current-turn
   attachments if present, else URL(s) in the query → WP-6 acquisition →
   vision pool walk with the planner's instruction as the prompt → return a
   `ChatToolResult` whose text is synthesized back exactly like `/research`
   results are today.
4. The Mercari scenario is *emergent*, not special-cased: user pastes a
   listing URL and asks 能不能買 → planner may run `/fetch`-style text
   analysis and/or `/visionlook` with the URL; the vision model's
   condition/discount reasoning is prompted («評估可見品況與合理折價», an
   instruction, not a rule table) — no marketplace-specific branch anywhere
   (G3).

### WP-6 — URL image acquisition (generic, bounded)

New helper in `vision_pool.py` (or `image_fetch.py`):

1. Fetch the page HTML (reuse the bridge's existing fetch plumbing used by
   `/fetch`; same UA/timeout conventions — locate `_exec_registered_command_chat_tool`'s
   fetch path when implementing).
2. Extract candidate image URLs **structurally**: `og:image` /
   `twitter:image` metas first, then largest-N `<img src>` candidates;
   resolve relative URLs. This is structural parsing (allowed), not
   open-world classification (G3) — *no* site-specific selectors.
3. Bounds: ≤3 images, ≤5 MB each pre-decode, total fetch wall-clock ≤20 s,
   every download under one deadline (G2). Non-image content-types skipped.
4. Downscale each through `encode_image_for_vision` before pooling.
5. Failure → return a text note listing what couldn't be fetched; the
   planner/answer path degrades gracefully.

### WP-7 — image translation replacement A/B (user directive 2026-07-05)

The current two-step (qwen2.5vl OCR → qwen3:14b translate,
`image_translate.py:183-259`) exists **only because** no multimodal model was
available then. Evaluate one-step cloud multimodal:

1. Add a vision-pool renderer variant: single prompt asking for
   `{"source_language": …, "translation": …}` JSON directly from the image
   (same contract as `_parse_translation_json`, `image_translate.py:165-180`,
   so the Telegram/web render layers don't change).
2. A/B on ~10 real images from recent use (screenshots with JP text, dense
   menus, handwriting): compare fidelity of transcription+translation,
   latency, and failure modes vs the local two-step. Judge by reading, not a
   metric script.
3. If cloud wins: `_handle_image_translation` routes to the vision pool
   first with the local two-step as fallback (pool-walk gives this for free
   when `local` sits last in `vision_pool`); the two-step code remains as
   the `local` pool member's implementation detail, not deleted.
4. If cloud loses or ties: keep local as primary, record the A/B outcome in
   §8 and close this WP — the vision pool still serves chat.

### WP-8 — tests + verification matrix

- pytest (single `.venv/bin/python -m pytest …` invocations):
  WP-1 settings tests; WP-2 walk tests; `Attachment`/`WebCommandRequest`
  already covered — add a chat-mode attachment fixture; planner prompt test
  asserting the vision tool line renders from a fake registry row; WP-6
  extractor test on a static HTML fixture.
- Live: WP-3 probe log; browser golden path (chat upload → observation →
  answer), Mercari URL flow, translation regression, settings save/reload
  round-trip.
- Bridge restart for manual verification: never touch the manually-run
  bridge on 8781 — verify on a throwaway port (8799) per collab rules.
- Update `VERIFICATION_MATRIX.md` with the new rows.

Suggested order: WP-3 (certify) → WP-1 → WP-2 → WP-5a → WP-4 → WP-5b → WP-6
→ WP-7 → WP-8 throughout.

---

## 6. Acceptance criteria

1. Chat mode accepts an image; a vision observation appears and the answer
   uses it; history stays text-only (`ChatTurn` schema untouched).
2. Text-only requests never consume vision-pool quota; image requests draw
   from the vision pool with rotation (two independent rotation cursors).
3. Settings UI shows and persists the vision pool (order, enable, model);
   a legacy `llm_pool.json` without vision keys loads with defaults.
4. Mercari-style flow works end-to-end via planner judgment with zero
   marketplace-specific code (grep proves no such branch).
5. Every default vision-pool member is live-certified (§WP-3 log in §8).
6. All new HTTP paths have bounded waits (G2) — reviewed explicitly.
7. WP-7 A/B verdict recorded; image translation routed accordingly.

## 7. Out of scope

Telegram-side chat vision (web first; Telegram photo flow already has its
own path), video/audio modalities, image *generation*, per-conversation
image memory beyond the textual observations.

## 8. Progress log

- 2026-07-05: plan written; issue #71 filed; provider capability
  web-verified (§1.2); live certification pending (WP-3).
- 2026-07-05: `big-pickle-vision` live-probed on zen API → "Model not
  supported"; user decision: drop big_pickle from the vision pool entirely
  (gemini / mistral / local only).
- 2026-07-05: settings 端同步移除 big_pickle 視覺選項——`_ALL_VISION_PROVIDERS`
  / `_default_vision_providers` / `vision_model_options_for_provider` 三處
  （`llm_pool_settings.py`），舊 config 含 big_pickle 者由 loader 靜默過濾；
  回歸測試 `test_vision_pool_never_offers_big_pickle`。
- 2026-07-05: **Deferred follow-up（以後有空再做，本輪不實作）**：Telegram
  拍照翻譯尚未統一到視覺池——`telegram_bot.py:970` 仍走
  `build_image_ocr_translate_renderer_from_settings`（本地 qwen2.5vl
  OCR→翻譯兩段式）。目標：比照 web 端 `_handle_image_translation`
  （`command_bridge.py:4530`，vision pool 優先＋本地 OCR fallback）統一走
  vision pool、保留本地 fallback。屆時把本項自 §7 Out of scope 移入 WP。
