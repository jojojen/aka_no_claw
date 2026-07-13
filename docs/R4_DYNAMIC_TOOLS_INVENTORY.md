# R4.0 — Dynamic-Tool Pipeline Responsibility / Threat / Resource Inventory

Last reviewed: 2026-07-13
Status: Current — R4.0 deliverable; update the "current site" column as
R4.1–R4.8 move code.
Owner area: dynamic-tools

Workstream R4 (issue [#76](https://github.com/jojojen/aka_no_claw/issues/76)),
stage R4.0 of `docs/P1_ENGINEERING_HARDENING_IMPLEMENTATION_PLAN.md` §12.

This is the pre-extraction map of `src/openclaw_adapter/dynamic_tools.py`
(≈3340 lines) — the `/new` self-writing-tool pipeline. It records, for every
responsibility: which of the plan's target modules it belongs to, the security
threats and machine resources it touches, and the compatibility contract every
later extraction slice must preserve. The public import surface below is pinned
by `tests/test_dynamic_tools_boundaries.py`.

The behavioral contract (repair budget, syntax gate, reuse/catalog metrics,
grounding, validator gating, secret-stripping) is already pinned by the ~90
tests in `tests/test_dynamic_tools.py`; R4.0 adds only the **module-boundary**
pin (public surface + separation-of-concerns invariants) that free-function
extraction into a package must not break.

## 0. Pipeline recap (`DynamicToolRunner.run_detailed`)

1. reuse-check — ask the model whether an existing manifest tool fits;
2. generate — methodology RAG + static hard rules + forced PLAN→code;
3. install `# requires:` packages into a dedicated venv;
4. execute under guardrail (`shell=False`, timeout, cwd, CLEAN_ENV strips
   secrets);
5. self-repair loop (≤3); `ModuleNotFoundError` auto-installs without burning a
   try;
6. on success — extract answer, register in manifest;
7. failure distillation — if it took ≥2 generations, abstract the mistake into a
   general rule and upsert into the `codegen_knowledge` RAG.

Default mode is local / free (Ollama qwen tier cascade). `OPENCLAW_CODEGEN_BACKEND=opencode`
routes generation/repair/validation to OpenCode Big Pickle while keeping
OpenClaw's execution guardrails. See `docs/NEW_DYNAMIC_TOOLS_PROGRESS.md` for the
acceptance benchmarks and live-verification history.

## 1. Target module map (plan §12)

Column "current site" = symbol in today's monolithic `dynamic_tools.py`. After
each extraction slice, update it to the new `dynamic_tools/<module>.py`.

### 1.1 `specification.py` — typed immutable spec + result envelope (R4.2)

| Responsibility | Current site |
|---|---|
| Trace envelope | `AttemptTrace`, `TaskTrace(.record/.to_dict)` |
| Result / plan value objects | `DynamicToolResult`, `ReusePlan`, `SearchGroundingResult`, `SearchGroundingBudgetExhausted` |
| Request normalization / slug | `_normalize_request`, `_make_slug`, `_split_request_intent` |
| Model-output parsing | `_extract_code`, `_extract_meta`, `_extract_answer`, `_extract_api_struct`, `_defaults_schema_from_code`, `_load_json_object` |
| Small pure helpers | `_coerce_nonneg_int`, `_utc_now_iso`, `_tail`, `_first_line` |

Threats: model-output parsing is an injection boundary (fenced-code / meta-JSON
extraction from untrusted LLM text). Resources: none.

### 1.2 `knowledge_context.py` — bounded RAG context (R4.2)

| Responsibility | Current site |
|---|---|
| Reference grounding | `_ground_references`, `_distill_reference_texts`, `_references_block` |
| Rule selection | `_rules_block`, `_load_rules_split`, `_keyword_fallback_rules`, `_merge_keyword_topicals`, `_mark_knowledge_applied` |
| Search grounding + budget | `_needs_search_grounding`, `_search_ground`, `_explore_api`, `_load_search_state`, `_save_search_state`, `_effective_search_limit`, `_current_search_limit`, `grant_search_extension` |

Threats: fetches untrusted web/API text into the prompt (SSRF-ish; already skips
engine-internal/PDF URLs). Resources: knowledge sqlite reads, network fetches,
`search_state` json (daily-reset budget). Contract: budget clamps and resets by
UTC day; failures fail-open but still burn budget (pinned by existing tests).

### 1.3 `providers.py` — provider protocol + text clients (R4.3)

| Responsibility | Current site |
|---|---|
| Protocol | `TextGenerationClient` |
| Clients | `OllamaTextClient`, `OpenCodeTextClient`, `OpenCodeCliTextClient`, `MistralTextClient`, `NvidiaTextClient` |
| Probes / builders | `probe_ollama`, `probe_opencode`, `probe_opencode_cli`, `build_research_cloud_text_client`, `_build_local_validator`, `_build_mistral_client`, `_select_model`, `_opencode_cli_model` |
| Provider errors / classifiers | `CloudBackendUnavailable`, `_is_thinking_model`, `_is_truncation_error` |

Threats: cloud API keys in env; CLI subprocess isolation (`HOME`/`CLAUDE_CONFIG_DIR`
sandboxed so `/new` doesn't read global CLAUDE.md). Resources: network, HTTP
retries, subprocess (`opencode run`). Contract: deterministic-failure fakes must
be substitutable (plan PR R4.3).

### 1.4 `safety.py` — static capability/safety policy (R4.4)

| Responsibility | Current site |
|---|---|
| Package allow/deny | `_is_safe_pkg`, `_is_approved_pkg` |
| Syntax gate (repair without burning a generation) | `_pass_syntax_gate`, `_syntax_error`, `_ensure_stdlib_imports` |
| Sandbox-wrapper failure classification | `_sandbox_wrapper_failed` |

Threats: THE trust boundary — decides which imports/packages are allowed before
code runs. Must stay **generator-independent** (a generated tool cannot widen
its own allowlist). Resources: none (pure static analysis / `ast`).

### 1.5 `sandbox.py` — execution, resource limits, cleanup (R4.5)

| Responsibility | Current site |
|---|---|
| Guarded execution | `_execute` (`shell=False`, timeout, cwd) |
| Secret-stripped env | `_clean_env` |
| Per-tool venv | `_venv_dir`, `_venv_python`, `_ensure_venv`, `_pip_install`, `_parse_requires` |
| Install+run orchestration | `_install_and_execute` |
| URL fetch helper | `_fetch_url_text` |
| Tools dir resolution | `_resolve_tools_dir` |

Threats: arbitrary-code execution surface. `_clean_env` strips `OPENCLAW_*` /
token secrets (pinned). Resources: subprocess, filesystem (`generated_tools/`,
venv), network (pip, fetch), timeouts. Contract: every terminal state cleans up
child processes / temp artifacts (plan PR R4.5).

### 1.6 `repair.py` — bounded repair controller (R4.6)

| Responsibility | Current site |
|---|---|
| Generate→execute→repair loop | `_generate_with_repair` |
| Codegen calls | `_generate_code`, `_repair_code`, `_build_codegen_prompt` |

Threats: unbounded repair = resource exhaustion. Contract: repair ≤3, identical
repair escalates tier early, repeated ineffective repair stops (pinned).
Resources: repeated provider calls, venv installs.

### 1.7 `evaluation.py` — generator-independent evaluation (R4.7)

| Responsibility | Current site |
|---|---|
| Answer validation | `_validate_answer`, `_run_one_validation`, `_prewarm_validator` |
| Feasibility preflight | `_preflight` |
| Tool-type classification | `_classify_tool_type` |
| Benchmarks | `_numbers`, `_check_numeric`, `_check_direction`, `load_benchmarks`, `run_benchmarks` |

Threats: a generator must not be able to edit/bypass its verifier; successful
execution is never proof of correctness. Validator fails **open** on its own
exception but respects a cap (pinned). Resources: validator provider calls.

### 1.8 `catalog.py` — versioned catalog / artifact metadata (R4.8)

| Responsibility | Current site |
|---|---|
| Manifest I/O | `_manifest_path`, `_load_manifest`, `_save_manifest`, `_register_manifest` |
| Reuse selection | `_pick_reusable`, `_existing_slug_for`, `_reuse`, `_extract_params`, `_apply_presentation` |
| Reuse/failure metrics | `_catalog_tool_type`, `_record_catalog_outcome` |

Threats: reuse of a persisted tool must re-validate (a poisoned manifest entry
can't shortcut the verifier). Resources: `manifest.json`, `generated_tools/`.
Contract: demoted/blocked tools are skipped and regenerated; old catalog reloads
stay compatible (pinned).

### 1.9 `service.py` — thin facade / orchestration (R4.8)

| Responsibility | Current site |
|---|---|
| Public entrypoints | `DynamicToolRunner.run`, `.run_detailed`, `.plan_for_text`, `.run_reuse_plan`, `.run_tool_step` |
| Formatting / labels | `_format_result`, `backend_label`, `_cmd_list_tools`, `_cmd_delete_tool` |
| Cloud failover | `_trigger_cloud_failover_restart` |
| Construction | `build_dynamic_tool_runner_from_settings`, `_build_runner_with_client` |
| Self-test | `_selftest_main` |

`DynamicToolRunner` is the orchestrator that will end up delegating to the eight
collaborators above; `dynamic_tools.py` becomes a thin re-export facade.

## 2. Public import surface (frozen — pinned by `test_dynamic_tools_boundaries.py`)

Every name below is imported from `openclaw_adapter.dynamic_tools` by another
`src/` module or by a test. After extraction, `dynamic_tools.py` must re-export
all of them unchanged.

Consumed by `src/`:

- `build_dynamic_tool_runner_from_settings` — telegram_bot
- `DynamicToolRunner`, `OllamaTextClient`, `_resolve_tools_dir` — command_bridge
- `OpenCodeTextClient`, `probe_opencode`, `_build_mistral_client` — command_bridge, fix_command, natural_language, goal_planner
- `MistralTextClient`, `NvidiaTextClient` — command_bridge, natural_language
- `build_research_cloud_text_client`, `CloudBackendUnavailable` — sns_tools, research_telegram
- `_extract_code` — fix_command

Additionally consumed by tests: `DynamicToolResult`, `ReusePlan`,
`SearchGroundingBudgetExhausted`, `OpenCodeCliTextClient`, `_extract_answer`,
`_check_numeric`, `_check_direction`, `_syntax_error`, `_is_truncation_error`,
`_defaults_schema_from_code`, `_ensure_stdlib_imports`, `probe_ollama`.

## 3. Separation-of-concerns invariants (must survive R4.1–R4.8)

These are the plan §12 mandates, restated as testable rules:

1. **Generation, safety, execution, repair, evaluation stay separate modules.**
   A generated tool cannot import or call the safety/evaluation modules to widen
   its own allowlist or approve its own answer.
2. **Safety is generator-independent.** `_is_safe_pkg` / `_is_approved_pkg` /
   syntax gate decide admissibility from the code text alone, with no reference
   to the model that produced it.
3. **Successful execution ≠ correctness.** The evaluation module gates answers
   independently; the reuse path re-validates before returning.
4. **Repair is bounded.** ≤3 attempts, identical-repair early escalation,
   repeated-ineffective-repair stop.
5. **Sandbox cleans up on every terminal state** and runs with secrets stripped
   (`_clean_env`).
6. **Facade stability.** `openclaw_adapter.dynamic_tools` keeps re-exporting the
   §2 surface; no consumer edits required to land an extraction slice.

Code-motion slices say `No intended semantic change`; any behavior change stops
and splits into a separate issue/PR after the facade is stable.

## 4. Delivery slices (plan §12)

- [ ] R4.0 — this inventory + boundary/characterization tests.
- [ ] R4.2 — `specification.py` + `knowledge_context.py` (typed spec, bounded RAG).
- [ ] R4.3 — `providers.py` (protocol + deterministic-failure fakes).
- [ ] R4.4 — `safety.py` (static policy + machine-readable rejection reasons).
- [ ] R4.5 — `sandbox.py` (resource limits + cleanup for every terminal state).
- [ ] R4.6 — `repair.py` (bounded repair + repeated-attempt detection).
- [ ] R4.7 — `evaluation.py` (generator-independent eval + discriminating tests).
- [ ] R4.8 — `catalog.py` + thin `service.py` facade.

Handoff/progress log lives in `docs/NEW_DYNAMIC_TOOLS_PROGRESS.md` (§R4).
