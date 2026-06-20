# Documentation Drift Checklist

Last reviewed: 2026-06-20
Owner area: agent-maintenance

Run this before any push that touches docs, subsystem status, entry points, or
cross-repo wiring. It verifies the five linked truth sources still agree:
`README.md` ↔ `SYSTEM_MANIFEST.yaml` ↔ `CURRENT_STATE.md` ↔ `TASK_ROUTING.md` ↔
`DOCS_INDEX.md`. See [DOCUMENTATION_GOVERNANCE.md](DOCUMENTATION_GOVERNANCE.md)
for who owns what.

## A. Cross-source consistency

- [ ] Every subsystem in `CURRENT_STATE.md` exists in `SYSTEM_MANIFEST.yaml`
  `subsystems:` with the **same status value** (`shipped`/`beta`/`partial`/...).
- [ ] Status vocabulary used anywhere matches `SYSTEM_MANIFEST.yaml`
  `status_vocabulary`.
- [ ] Entry points (CLI / Telegram commands) listed in `CURRENT_STATE.md`,
  `SYSTEM_MANIFEST.yaml`, and `TASK_ROUTING.md` match each other and the code.
- [ ] Repos + default branches in `SYSTEM_MANIFEST.yaml` `repos:` match reality
  (`aka_no_claw`=main, `price_monitor_bot`=master, `sns_monitor_bot`=main,
  `reputation_snapshot`=master).
- [ ] `README.md` does not describe planned behavior as shipped; anything not in
  `CURRENT_STATE.md` as `shipped`/`beta` is not stated as live in README.
- [ ] Required reading order in `AGENT_ONBOARDING.md` matches
  `SYSTEM_MANIFEST.yaml` `agent_rules.required_reading_order`.

## B. Index integrity

- [ ] Every `*.md` under `docs/` (excluding `archive/`) has a row in
  `DOCS_INDEX.md`.
- [ ] Every row in `DOCS_INDEX.md` points to a file that exists.
- [ ] Every doc moved to `docs/archive/` is listed in `archive/README.md` and is
  no longer listed as `Current` in `DOCS_INDEX.md`.
- [ ] `DOC_AUDIT.md` reflects the current file set (no audited file missing, no
  archived file still listed as active).

## C. Header hygiene

- [ ] Each stateful doc has `Last reviewed`, `Status`, `Owner area` (governance §2).
- [ ] Docs changed in this push have a refreshed `Last reviewed` date.

## D. Canonical location

- [ ] No new third doc was created for a domain that already has a canonical owner
  (`DOC_AUDIT.md` canonical-location table); companions are cross-linked instead.

## E. Mechanical checks

Run from repo root:

```bash
# Broken intra-docs links (lists referenced files that do not exist)
grep -rhoE '\]\(([^)]+\.(md|yaml))\)' docs/ README.md \
  | sed -E 's/.*\(([^)]+)\).*/\1/' | sort -u \
  | while read -r p; do
      case "$p" in
        ../*) f="$p" ;;
        *) f="docs/$p" ;;
      esac
      [ -e "$f" ] || [ -e "$p" ] || echo "MISSING: $p"
    done

# Docs under docs/ not referenced in DOCS_INDEX.md
for f in docs/*.md; do
  b=$(basename "$f")
  [ "$b" = "DOCS_INDEX.md" ] && continue
  grep -q "$b" docs/DOCS_INDEX.md || echo "UNINDEXED: $b"
done
```

Both loops should print nothing. Any output is drift to fix before pushing.
