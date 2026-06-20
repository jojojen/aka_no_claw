# Documentation Governance

Last reviewed: 2026-06-20
Owner area: agent-maintenance

How documentation in this repo is structured, where truth lives, and how to keep
it from drifting. Read this before adding, moving, or retiring any doc.

## 1. Authoritative truth sources (which docs are authoritative)

Exactly one document owns each kind of truth. When two docs disagree, the owner wins.

| Question | Authoritative source |
|---|---|
| What is the system, machine-readable? | `../SYSTEM_MANIFEST.yaml` |
| What is the architecture / data ownership / flows? | `SYSTEM_MAP.md` (human) + `../SYSTEM_MANIFEST.yaml` (machine) |
| What is the runtime status of each subsystem? | `CURRENT_STATE.md` |
| Where do I edit for a given task? | `TASK_ROUTING.md` |
| How do I verify a change? | `VERIFICATION_MATRIX.md` |
| Where is every doc and what is it? | `DOCS_INDEX.md` |
| House rules / required reading order? | `../Constitution.md`, `AGENT_ONBOARDING.md` |

Everything else (plans, specs, methodology, usage, progress) is **supporting**
documentation, not truth. Supporting docs must not contradict the sources above;
if they do, the supporting doc is wrong.

### Path convention

- Root level: `Constitution.md`, `README.md`, `SYSTEM_MANIFEST.yaml` only. These
  are stable, tool- and human-entry anchors.
- `docs/`: all other human-readable truth and supporting docs.
- `docs/archive/`: frozen, superseded docs (historical context only).

Do not scatter new truth files at the repo root; add them under `docs/` and list
them in `DOCS_INDEX.md`.

## 2. Lifecycle stages

Every stateful doc carries a header:

```text
Last reviewed: YYYY-MM-DD
Status: <stage>
Owner area: <area>
```

| Stage | Meaning | Lives in |
|---|---|---|
| `Current` | Describes present shipped/active reality. Trust it. | `docs/` |
| `Needs review` | Was current; may have drifted. Verify before relying. | `docs/` |
| `Planned` | Describes intended, not-yet-shipped behavior. | `docs/` |
| `Historical` | Frozen snapshot or superseded plan. Context only. | `docs/archive/` |

Owner areas: `price` / `liquidity` / `research` / `dynamic-tools` / `sns` /
`reputation` / `opportunity` / `knowledge` / `quiz` / `telegram` / `dashboard` /
`operations` / `verification` / `architecture` / `agent-maintenance`.

## 3. Update rules (how docs change with the system)

When you change the system, update the matching truth source in the **same change**:

| You changed... | Update |
|---|---|
| Subsystem shipped status, entry points, or data ownership | `CURRENT_STATE.md` + `SYSTEM_MANIFEST.yaml` |
| Architecture, flows, repo boundaries | `SYSTEM_MAP.md` + `SYSTEM_MANIFEST.yaml` |
| Where a task is handled (moved/renamed module) | `TASK_ROUTING.md` |
| How a change is verified (entry points, test paths) | `VERIFICATION_MATRIX.md` |
| Added/moved/retired a doc | `DOCS_INDEX.md` + `DOC_AUDIT.md` |
| Cross-repo responsibility | `SYSTEM_MANIFEST.yaml` `repos:` + `CURRENT_STATE.md` |

Do not document planned behavior as shipped. Mark it `Planned`.

## 4. Adding new documentation (where new docs go)

1. Create the file under `docs/` with the header block from §2.
2. Pick the canonical location for its topic. If a doc already owns that domain
   (see `DOC_AUDIT.md` "canonical-location decisions"), extend it or add a
   cross-linked companion — do not start a parallel third doc.
3. Add a row to `DOCS_INDEX.md` (status, owner area, purpose) and `DOC_AUDIT.md`.

## 5. Retiring / handling outdated docs (how outdated docs are handled)

- Suspected stale but possibly relevant → set `Status: Needs review`; leave in `docs/`.
- Confirmed superseded or a frozen snapshot → `git mv` to `docs/archive/`, add a
  row to `archive/README.md`, and update `DOCS_INDEX.md` + `DOC_AUDIT.md`.
- Never delete useful historical context. Archive instead.

## 6. Safe-to-ignore for most work

For day-to-day feature work, `docs/archive/**` and any `Status: Historical` doc can
be skipped. Start from `AGENT_ONBOARDING.md` → the truth sources in §1.

## 7. Preventing drift

Run [DOC_DRIFT_CHECKLIST.md](DOC_DRIFT_CHECKLIST.md) before any push that touches
docs, entry points, subsystem status, or cross-repo wiring. It cross-checks
README ↔ SYSTEM_MANIFEST ↔ CURRENT_STATE ↔ TASK_ROUTING ↔ DOCS_INDEX.
