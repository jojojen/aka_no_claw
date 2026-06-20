# Documentation Change Template

Last reviewed: 2026-06-20
Status: Current
Owner area: agent-maintenance

Use this template for any change that adds, moves, retires, or edits
documentation, subsystem status, entry points, or cross-repo wiring. Copy the
checklist below into the PR description.

Governance and rules this enforces:

- [DOCUMENTATION_GOVERNANCE.md](DOCUMENTATION_GOVERNANCE.md) — truth ownership,
  lifecycle stages, where new docs go.
- [DOC_DRIFT_CHECKLIST.md](DOC_DRIFT_CHECKLIST.md) — manual cross-source check.
- CI: `.github/workflows/docs-health.yml` runs the three checkers automatically.

---

## What changed?

<!-- One or two lines describing the documentation change and why. -->

## Which truth docs changed?

Tick every authoritative source touched (see governance §1). If you changed one
side of a pair, you almost always need the other.

- [ ] `SYSTEM_MANIFEST.yaml`
- [ ] `docs/CURRENT_STATE.md`
- [ ] `docs/SYSTEM_MAP.md`
- [ ] `docs/TASK_ROUTING.md`
- [ ] `docs/VERIFICATION_MATRIX.md`
- [ ] `docs/DOCS_INDEX.md` (+ `docs/DOC_AUDIT.md` when adding/moving/retiring a doc)
- [ ] None — supporting docs only

## Drift checks passed?

Run locally before pushing (CI runs the same):

```
.venv/bin/python scripts/check_docs_health.py
.venv/bin/python scripts/check_manifest.py
.venv/bin/python scripts/check_doc_drift.py
```

- [ ] `check_docs_health.py` passed (indexing, metadata, archive, links)
- [ ] `check_manifest.py` passed (status vocabulary, repo/subsystem fields)
- [ ] `check_doc_drift.py` passed (manifest ↔ CURRENT_STATE aligned)

## Verification run?

- [ ] New docs added under `docs/` are listed in `DOCS_INDEX.md` with status + owner area
- [ ] Each stateful doc carries `Last reviewed:` and `Owner area:`
- [ ] Retired docs were `git mv`'d to `docs/archive/` and are not marked `Status: Current`
- [ ] If subsystem status/entry points changed, `SYSTEM_MANIFEST.yaml` and
      `CURRENT_STATE.md` were updated in the same change
