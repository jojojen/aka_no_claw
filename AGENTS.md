# AGENTS.md

This repository keeps its Codex-facing workflow in version control on purpose.

## Repo-local skill

- Primary skill: `ai/skills/aka-no-claw-workflow/SKILL.md`
- Install or refresh it into Codex with `scripts/install-codex-skills.ps1`
- Restart Codex after installation so the new skill is discovered

## Expectations

- Read `Constitution.md` before substantial work
- Keep `openclaw_adapter` assistant-specific
- Move reusable pricing or capture logic to the sibling repos that own it
- Run targeted pytest checks before finishing
