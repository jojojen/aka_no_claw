"""Typed value objects, output-protocol markers, and pure parsers for the
dynamic-tool (`/new`) pipeline.

R4.2 (issue #76) leaf module: depends only on the standard library, so every
other collaborator (``providers``, ``safety``, ``repair``, ``evaluation``,
``catalog``, ``service``) can import from it without cycles. See
``docs/R4_DYNAMIC_TOOLS_INVENTORY.md`` §1.1.

Holds the model-output contract (``===ANSWER===`` etc. markers, ``<think>`` /
code-fence stripping) and the immutable result/plan/trace types. The ``_extract_*``
parsers are the injection boundary between untrusted model text and the pipeline;
they are intentionally pure (no I/O, no runner state).
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ── output-protocol markers ──────────────────────────────────────────────────
ANSWER_START = "===ANSWER==="
ANSWER_END = "===END==="
_CODE_MARK = "===CODE==="
_PLAN_MARK = "===PLAN==="
_META_MARK = "===META==="
_API_STRUCT_START = "===API_STRUCT==="
_API_STRUCT_END = "===END_STRUCT==="
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL)


def _coerce_nonneg_int(value, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out >= 0 else default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class AttemptTrace:
    """One step of the troubleshoot-reflect-continue loop (#51).

    Records what was done (action), what came back (observation), how it was
    classified (reflection), and what the loop chose to do next (next_action)."""
    phase: int
    attempt: int
    action: str
    observation: str
    reflection: str = ""
    next_action: str = ""


@dataclass
class TaskTrace:
    """Structured trace of a `/new` run: the goal, every attempt, the budget
    snapshot, and why the loop stopped. Purely observational — recording it does
    not change loop behavior. Serializable via ``to_dict`` so PR2 can emit a
    compact continuation state from the same data."""
    goal: str
    attempts: list[AttemptTrace] = field(default_factory=list)
    # Total generations across the whole tier cascade. The cascade is
    # tier1(max_repairs) + tier2(max_repairs) + tier3(1), so the total limit is
    # 2*max_repairs+1 — using max_repairs here let `used` exceed `limit` once the
    # loop climbed a tier (review #51). `generations_*` are TOTAL; `tier_*`
    # describe budget within the current tier only.
    generations_used: int = 0
    generations_limit: int = 0
    tier: int = 1
    tier_limit: int = 3
    tier_generations_used: int = 0
    tier_generations_limit: int = 0
    search_used: int = 0
    search_limit: int = 0
    stop_condition: str = ""

    def record(self, *, phase: int, attempt: int, action: str, observation: str,
               reflection: str = "", next_action: str = "") -> None:
        self.attempts.append(AttemptTrace(
            phase=phase, attempt=attempt, action=action,
            observation=observation, reflection=reflection, next_action=next_action,
        ))

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "attempts": [
                {
                    "phase": a.phase, "attempt": a.attempt, "action": a.action,
                    "observation": a.observation, "reflection": a.reflection,
                    "next_action": a.next_action,
                }
                for a in self.attempts
            ],
            "budget": {
                "generations_used": self.generations_used,
                "generations_limit": self.generations_limit,
                "tier": self.tier,
                "tier_limit": self.tier_limit,
                "tier_generations_used": self.tier_generations_used,
                "tier_generations_limit": self.tier_generations_limit,
                "search_used": self.search_used,
                "search_limit": self.search_limit,
            },
            "stop_condition": self.stop_condition,
        }


@dataclass
class DynamicToolResult:
    ok: bool
    answer: str = ""
    slug: str = ""
    reused: bool = False
    generations: int = 0
    error: str = ""
    raw_stdout: str = ""
    trace: TaskTrace | None = None


@dataclass(frozen=True)
class SearchGroundingBudgetExhausted:
    count: int
    effective_limit: int
    soft_cap: int
    hard_cap: int
    granted_extra: int


@dataclass(frozen=True)
class SearchGroundingResult:
    block: str | None = None
    query_burned: bool = False
    budget_exhausted: SearchGroundingBudgetExhausted | None = None


@dataclass
class ReusePlan:
    """A non-executing decision the Chat/planner layer can act on (#52 live
    integration). ``action`` is one of:

    - ``none``           — no relevant existing tool; stay silent / defer to the
                           bot's default reply (don't spin codegen on chatter).
    - ``run``            — a *promoted* (trusted) tool matched; run it now.
    - ``confirm_reuse``  — a fresh (candidate/recovering) tool matched; ask the
                           user before reusing it.
    - ``confirm_generate`` — there was a relevance signal but no usable existing
                           tool; offer to generate a new one.

    ``match`` is the raw manifest entry (execution handle keyed by slug); it is
    never built from model output, so the planner cannot run arbitrary code."""
    action: str
    slug: str | None = None
    tool_type: str | None = None
    match: dict | None = None
    core: str = ""
    format_spec: str = ""


def _normalize_request(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).strip().lower()


def _extract_code(response: str) -> str:
    text = _THINK_RE.sub("", response or "").strip()
    if _CODE_MARK in text:
        code = text.split(_CODE_MARK, 1)[1]
    else:
        code = text
    fence = _FENCE_RE.search(code)
    if fence:
        code = fence.group(1)
    # strip any stray leading marker lines
    code = code.replace(ANSWER_END, ANSWER_END)  # no-op keep
    return code.strip() + "\n"


def _extract_meta(response: str) -> dict | None:
    """Parse the ===META=== JSON block (tool_type + param_schema) if present."""
    text = _THINK_RE.sub("", response or "")
    if _META_MARK not in text:
        return None
    seg = text.split(_META_MARK, 1)[1]
    if _CODE_MARK in seg:
        seg = seg.split(_CODE_MARK, 1)[0]
    return _load_json_object(seg)


def _defaults_schema_from_code(code: str) -> list | None:
    """Derive a param_schema from the tool's top-level ``DEFAULTS = {...}`` dict.

    The model's ===META=== block is unreliable (it sometimes omits param_schema
    entirely), but the parameterized code pattern always carries a literal
    DEFAULTS dict. Reading the keys/types straight from the AST gives a
    deterministic schema so any parameterized tool is reusable, regardless of
    what the model did or didn't put in META.
    """
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "DEFAULTS" for t in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            return None
        schema: list = []
        for key_node, val_node in zip(node.value.keys, node.value.values):
            if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                continue
            name = key_node.value
            try:
                val = ast.literal_eval(val_node)
            except (ValueError, SyntaxError):
                val = None
            kind = "number" if isinstance(val, (int, float)) and not isinstance(val, bool) else "string"
            schema.append({"name": name, "type": kind, "desc": name})
        return schema or None
    return None


def _extract_answer(stdout: str) -> str:
    if ANSWER_START not in stdout:
        return ""
    after = stdout.split(ANSWER_START, 1)[1]
    if ANSWER_END in after:
        after = after.split(ANSWER_END, 1)[0]
    return after.strip()


def _load_json_object(raw: str) -> dict | None:
    text = _THINK_RE.sub("", raw or "").strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            obj = json.loads(match.group(0))
            return obj if isinstance(obj, dict) else None
        except ValueError:
            return None


def _extract_api_struct(stdout: str) -> str:
    if _API_STRUCT_START not in stdout:
        return ""
    after = stdout.split(_API_STRUCT_START, 1)[1]
    if _API_STRUCT_END in after:
        after = after.split(_API_STRUCT_END, 1)[0]
    return after.strip()
