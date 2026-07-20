# AGENTS.md

## First Principle — General Correctness

1. **Correctness first means general correctness, not correctness for one case.**
   Never make the current example pass with hardcoded keywords, values, output
   text, or exception branches. Prefer a structural solution that removes
   special cases; a sound fix should normally reduce total code and branch
   count. If a proposed fix adds case-specific code, stop and redesign it.

2. **Research uncertainty before coding.** When the correct general solution
   is unclear, consult current primary sources and proven implementations before
   changing code. Use that evidence to define a general contract or design;
   never replace uncertainty with a case-specific hardcode.

3. **Use ASD-STE100 as the language principle for comments and documentation.**
   During development, write code comments, technical notes, and user-facing
   project documentation in ASD Simplified Technical English style: short direct
   sentences, active voice, one instruction per sentence where practical,
   consistent terms for the same concept, and concrete nouns instead of vague
   references. Avoid idioms, rhetorical phrasing, hidden assumptions, and
   unnecessary adjectives. When exact ASD-STE100 compliance is impractical,
   prefer clarity, consistency, and unambiguous technical meaning.

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
