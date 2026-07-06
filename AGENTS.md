# AGENTS.md

This repository keeps its Codex-facing workflow in version control on purpose.

## Repo-local skill

- Session-start external rules: read `https://raw.githubusercontent.com/jojojen/claude-collab-rules/main/SKILL.md`
  before substantial work, and revisit it before any `git push`, refactor, or
  production-impacting change.
- Primary skill: `ai/skills/aka-no-claw-workflow/SKILL.md`
- Install or refresh it into Codex with `scripts/install-codex-skills.ps1`
- Restart Codex after installation so the new skill is discovered

## Expectations

- Read `Constitution.md` before substantial work
- Follow `claude-collab-rules/SKILL.md` git protocol: before any `git push`, give the
  repo/files/subject summary and wait for explicit user confirmation.
- Keep `openclaw_adapter` assistant-specific
- Move reusable pricing or capture logic to the sibling repos that own it
- Run targeted pytest checks before finishing
- After debugging and fixing any error, distill the generalizable lesson and
  add it as a new entry to `CODEGEN_SEED` in
  `src/openclaw_adapter/knowledge_db.py` (generic technique only, never a
  domain-specific formula) — see `Constitution.md` §8/§11
