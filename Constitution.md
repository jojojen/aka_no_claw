# Project Constitution

Last updated: 2026-04-14

This file is the project constitution for this repository. Before starting any substantial work, read this file first, then read the relevant local docs such as `README.md` and `OPENCLAW_TCG_MONITOR_PLAN.md`.

The goal is simple: reduce avoidable mistakes, keep architecture clean, and make every change safe to extend.

## 1. Priority Order

When instructions conflict, use this order:

1. Explicit user request for the current task
2. This constitution
3. Existing project docs and established local patterns
4. General upstream style guides

If an explicit user request would introduce a serious security, data-loss, or architecture risk, pause and say so before proceeding.

## 2. Core Project Identity

This repo is not just a card-price bot.

- OpenClaw is the personal assistant entrypoint.
- Price tracking is one tool family the assistant can use.
- Reusable monitoring logic must stay separate from OpenClaw-specific adapters.
- Domain-specific modules must stay separate from the generic monitoring core.

Current separation:

- `assistant_runtime`: generic assistant runtime and tool registration
- `market_monitor`: generic monitoring, pricing, and source-catalog core
- `tcg_tracker`: TCG-specific matching and collection logic
- `openclaw_adapter`: OpenClaw / CLI / Telegram integration layer

Do not collapse these boundaries just to move faster.

## 3. Non-Negotiable Engineering Rules

- Use a virtual environment in `.venv` for Python work.
- Manage dependencies through `requirements.txt` and `requirements-dev.txt`.
- Keep sensitive values in `.env`; keep placeholders only in `.env.example`.
- Keep `.venv`, `.env`, runtime databases, caches, logs, and build artifacts out of git via `.gitignore`.
- Prefer small, testable modules over prompt-only logic for anything involving parsing, normalization, pricing, deduplication, or alerting.
- Do not hardcode secrets, chat IDs, tokens, cookies, or local machine paths in source files.
- Do not make destructive git or filesystem changes unless explicitly requested and clearly safe.
- Do not revert user changes you did not make.

## 4. Coding Style Baseline

Project-specific consistency wins over generic rules, but the baseline is:

- Optimize for readability first.
- Prefer explicit behavior over clever shortcuts.
- Keep functions focused and easy to explain.
- Use descriptive names instead of compressed names.
- Prefer straight-line control flow over deeply nested logic.
- Keep side effects obvious.
- Follow existing local patterns before introducing a new abstraction.

Python-specific expectations:

- Follow PEP 8 as the default style baseline.
- Use 4 spaces for indentation.
- Prefer parentheses for line wrapping instead of backslashes when practical.
- Use type hints where they improve clarity, especially at module boundaries.
- Add comments only when they explain intent, invariants, or non-obvious tradeoffs.
- Keep docstrings for public or non-obvious behavior; do not add noise docstrings that restate the code.

Formatting decisions should favor maintainability, not personal preference.

## 5. Architecture Rules

- Assistant entrypoints should stay thin; business logic belongs in reusable modules.
- Generic pricing or source-management logic belongs in `market_monitor`, not in TCG-only code.
- TCG-specific parsing, matching, aliases, and card heuristics belong in `tcg_tracker`.
- OpenClaw / Telegram / CLI wiring belongs in `openclaw_adapter`.
- Configuration loading belongs in runtime settings, not scattered through modules.
- Source metadata should be centralized and reusable. The source catalog is a shared contract, not ad hoc constants spread around the codebase.

Before adding a new module, first ask: is this assistant-specific, domain-specific, or generic?

## 6. Data Source and Monitoring Rules

Different source classes have different jobs. Do not treat all sources as interchangeable.

- `official_metadata` sources are for normalization and verification, not direct price estimation.
- `specialty_store` sources are for higher-trust ask and buy references.
- `marketplace` and `market_content` sources are for market depth, live opportunity scanning, and trend signals.

For collectors and scrapers:

- Favor public pages and documented behavior over internal or private APIs.
- Respect terms, access limits, and site stability.
- Use low-frequency polling, caching, deduplication, and retries.
- Preserve source provenance so each price or listing can be traced back to its origin.
- Keep raw payload storage or equivalent debugging breadcrumbs when it materially helps parser maintenance.

If a source is useful only for metadata, do not silently mix it into fair-value calculations.

## 7. Configuration and Secrets

- Secrets must live in `.env`, never in committed code.
- `.env.example` should document required variables without real values.
- Deployment-specific config belongs in environment variables.
- Internal code wiring and stable defaults belong in code, not in scattered ad hoc config files.
- Any new integration that needs credentials must update both settings loading and `.env.example`.

Use this litmus test: could the repository be published publicly right now without exposing credentials? If not, fix that before proceeding.

## 8. Testing and Verification

- New behavior should come with tests when feasible.
- Bug fixes should include a regression test when practical.
- Shared-core changes require stronger verification than isolated docs or copy changes.
- Run targeted verification after edits; run broader verification when touching shared paths.
- If verification is skipped or blocked, say so explicitly in the final report.

Current repo defaults:

- Keep tests in `tests/`.
- Use the `src/` layout.
- Prefer `python -m pytest` or the venv-local equivalent.

Tests are not optional decoration. They are how we keep collectors, pricing logic, and assistant tools from drifting.

## 9. Git and Change Hygiene

- Make the smallest coherent change that solves the task.
- Avoid drive-by refactors unless they directly reduce risk for the task at hand.
- Inspect surrounding code before editing; local consistency matters.
- Do not mix unrelated fixes into the same change without a clear reason.
- Update docs when behavior, workflow, or configuration changes.
- Keep generated files, caches, and local databases out of commits unless explicitly intended.

## 10. Common Severe Mistakes to Avoid

- Blurring generic monitoring logic with assistant-specific glue code
- Hardcoding secrets or local-only settings
- Mixing metadata-only sources into price calculations
- Treating scraped text as canonical without normalization
- Shipping parser changes without a realistic verification path
- Over-engineering before the simple path is proven
- Refactoring unrelated code while the worktree may already contain user changes
- Adding dependencies without a clear need and without updating dependency files
- Relying on hidden assumptions instead of stating them
- Leaving ambiguous behavior undocumented when the ambiguity affects price alerts or matching

## 11. Pre-Task Checklist

Before any substantial task:

1. Read this file.
2. Read the relevant local docs and affected modules.
3. Confirm which layer the change belongs to.
4. Check whether config, secrets, or gitignore entries are affected.
5. Decide the minimum safe verification plan.
6. Prefer the smallest change that preserves clean boundaries.

Before finishing a task:

1. Re-read the changed files for consistency.
2. Run the chosen verification.
3. Check that no secrets or local artifacts were introduced.
4. Summarize assumptions, outcomes, and any remaining risk clearly.

## 12. External Style References

These sources inform the constitution. Project-specific rules above still take precedence here.

- PEP 8 says readability and consistency matter, while local project consistency takes precedence over generic style.
- PEP 20 emphasizes that explicit, simple, readable code is preferable and that ambiguous situations should not be guessed through silently.
- PEP 257 frames conventions as a foundation for maintainability and consistent habits.
- Python Packaging guidance recommends using an isolated `.venv` and excluding it from version control.
- pytest good practices recommend a `tests/` directory and strongly suggest a `src/` layout for new projects.
- `gitignore` exists to keep intentionally untracked generated files out of Git.
- Twelve-Factor config guidance supports keeping deploy-varying config in environment variables rather than code.

References:

- https://peps.python.org/pep-0008/
- https://peps.python.org/pep-0020/
- https://peps.python.org/pep-0257/
- https://packaging.python.org/en/latest/guides/installing-using-pip-and-virtual-environments/
- https://docs.pytest.org/en/stable/explanation/goodpractices.html
- https://git-scm.com/docs/gitignore
- https://12factor.net/config
