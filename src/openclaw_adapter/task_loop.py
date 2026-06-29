"""Bounded troubleshoot-reflect-continue loop with resumable continuation state.

#51 PR2. Generalizes `/new`'s repair-loop pattern (see
``dynamic_tools.TaskTrace``) into a small bounded engine that any short,
*allowlisted* workflow can drive. It is deliberately NOT a general agent:

- it only invokes steps from a caller-supplied allowlist,
- it runs at most ``max_steps``,
- it has explicit stop conditions and never retries indefinitely.

Its one capability beyond the #50 straight-line plan: when the step budget is
reached before the goal is met, it emits a compact :class:`ContinuationState`
(the musubi ``anchored-summary`` text schema — no ledger / replay machinery) and
can resume from it *at the next action* instead of restarting. That resume path
is the live client the #51 continuation mechanism needs; the loop itself is
single-process and synchronous, like ``/new`` and chat.

The threshold modelled here is the step budget. A token/context threshold can be
mapped onto the same pause-and-emit mechanism later by lowering ``max_steps`` or
passing a custom ``should_pause`` predicate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class StepOutcome:
    """Result of executing one allowlisted step.

    ``done`` ends the loop with success. ``failed`` marks a step that did not
    satisfy its sub-goal (its ``reflection`` is recorded as an attempted fix).
    ``lesson`` carries an abstract, transferable rule when a failure mode is
    resolved — the loop collects these for distillation (#51 distillation AC)."""
    observation: str
    done: bool = False
    failed: bool = False
    reflection: str = ""
    lesson: str = ""


@dataclass
class LoopContext:
    """Read-only view handed to the decider and each step."""
    goal: str
    constraints: str
    completed: list[str]
    history: list[StepOutcome]
    steps_used: int
    max_steps: int


# A decider picks the NAME of the next step (must be in the allowlist). A step
# executes and returns its outcome. Both see only the LoopContext.
Decider = Callable[[LoopContext], str]
Step = Callable[[LoopContext], StepOutcome]


@dataclass
class ContinuationState:
    """Compact, resumable snapshot — the musubi anchored-summary schema.

    Holds exactly what a resume needs: the goal and constraints, what is already
    done (so it is not redone), the current status and attempted fixes, the
    budget, the next action to take, and the stop condition."""
    goal: str
    constraints: str = ""
    completed: list[str] = field(default_factory=list)
    current_status: str = ""
    attempted_fixes: list[str] = field(default_factory=list)
    budget: dict = field(default_factory=dict)
    next_action: str = ""
    stop_condition: str = ""

    def render(self) -> str:
        """Render as the anchored-summary text a human or a model can resume from."""
        budget_bits = []
        if "steps_used" in self.budget:
            budget_bits.append(
                f"steps {self.budget.get('steps_used')}/{self.budget.get('steps_limit')}"
            )
        if "search_used" in self.budget:
            budget_bits.append(
                f"search {self.budget.get('search_used')}/{self.budget.get('search_limit')}"
            )
        completed_lines = [f"- {c}" for c in self.completed] or ["- (none)"]
        fix_lines = [f"- {a}" for a in self.attempted_fixes] or ["- (none)"]
        lines = [
            f"Goal: {self.goal}",
            f"Constraints: {self.constraints or 'none'}",
            "Completed:",
            *completed_lines,
            f"Current status: {self.current_status or 'n/a'}",
            "Attempted fixes:",
            *fix_lines,
            f"Budgets: {', '.join(budget_bits) if budget_bits else 'n/a'}",
            f"Next: {self.next_action or '(none)'}",
            f"Stop if: {self.stop_condition or '(unset)'}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "constraints": self.constraints,
            "completed": list(self.completed),
            "current_status": self.current_status,
            "attempted_fixes": list(self.attempted_fixes),
            "budget": dict(self.budget),
            "next_action": self.next_action,
            "stop_condition": self.stop_condition,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ContinuationState":
        return cls(
            goal=str(data.get("goal", "")),
            constraints=str(data.get("constraints", "")),
            completed=list(data.get("completed") or []),
            current_status=str(data.get("current_status", "")),
            attempted_fixes=list(data.get("attempted_fixes") or []),
            budget=dict(data.get("budget") or {}),
            next_action=str(data.get("next_action", "")),
            stop_condition=str(data.get("stop_condition", "")),
        )


@dataclass
class LoopResult:
    """Outcome of a (possibly partial) loop run.

    ``done`` means the goal was met. Otherwise ``state`` carries the resumable
    :class:`ContinuationState`. ``lessons`` are abstract rules worth distilling."""
    done: bool
    outcomes: list[StepOutcome]
    lessons: list[str]
    stop_condition: str
    state: ContinuationState | None = None


def sequential_decider(plan: list[str]) -> Decider:
    """A deterministic decider: walk ``plan`` in order, skipping steps already
    completed. Returns ``""`` when the plan is exhausted. Useful as the default
    bounded policy and for tests; an LLM decider can be swapped in later."""
    def decide(ctx: LoopContext) -> str:
        done_actions = {c.split(":", 1)[0] for c in ctx.completed}
        for action in plan:
            if action not in done_actions:
                return action
        return ""
    return decide


class BoundedTaskLoop:
    """Run an allowlisted workflow under a hard step budget, pausing into a
    resumable :class:`ContinuationState` when the budget is hit before success."""

    def __init__(
        self,
        goal: str,
        *,
        steps: dict[str, Step],
        decider: Decider,
        max_steps: int = 5,
        constraints: str = "",
        extra_budget: dict | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        self.goal = goal
        self.steps = dict(steps)
        self.decider = decider
        self.max_steps = max_steps
        self.constraints = constraints
        # Budget fields surfaced in the continuation state beyond step count
        # (e.g. search usage), so a resume sees every enforced limit.
        self.extra_budget = dict(extra_budget or {})

    def run(self, resume: ContinuationState | None = None) -> LoopResult:
        """Execute the loop. When ``resume`` is given, completed steps are NOT
        re-run; execution begins at ``resume.next_action`` and continues."""
        completed: list[str] = list(resume.completed) if resume else []
        attempted_fixes: list[str] = list(resume.attempted_fixes) if resume else []
        history: list[StepOutcome] = []
        lessons: list[str] = []
        forced = (resume.next_action or None) if resume else None
        steps_used = 0

        while steps_used < self.max_steps:
            ctx = LoopContext(
                goal=self.goal, constraints=self.constraints,
                completed=completed, history=history,
                steps_used=steps_used, max_steps=self.max_steps,
            )
            action = forced or self.decider(ctx)
            forced = None
            if not action:
                return LoopResult(
                    done=False, outcomes=history, lessons=lessons,
                    stop_condition="no further action (plan exhausted without success)",
                    state=self._snapshot(
                        completed, attempted_fixes, history, steps_used,
                        next_action="", stop="plan exhausted without success",
                    ),
                )
            if action not in self.steps:
                # Guardrail: the decider tried to leave the allowlist.
                return LoopResult(
                    done=False, outcomes=history, lessons=lessons,
                    stop_condition=f"non-allowlisted action {action!r}",
                    state=self._snapshot(
                        completed, attempted_fixes, history, steps_used,
                        next_action="", stop=f"non-allowlisted action {action!r}",
                    ),
                )
            outcome = self.steps[action](ctx)
            steps_used += 1
            history.append(outcome)
            completed.append(f"{action}: {outcome.observation}")
            if outcome.failed and outcome.reflection:
                attempted_fixes.append(f"{action}: {outcome.reflection}")
            if outcome.lesson:
                lessons.append(outcome.lesson)
            if outcome.done:
                return LoopResult(
                    done=True, outcomes=history, lessons=lessons,
                    stop_condition="goal satisfied", state=None,
                )

        # Budget hit before success: determine what we WOULD do next (without
        # executing it) and emit a resumable continuation state.
        ctx = LoopContext(
            goal=self.goal, constraints=self.constraints,
            completed=completed, history=history,
            steps_used=steps_used, max_steps=self.max_steps,
        )
        next_action = self.decider(ctx)
        return LoopResult(
            done=False, outcomes=history, lessons=lessons,
            stop_condition="step budget reached",
            state=self._snapshot(
                completed, attempted_fixes, history, steps_used,
                next_action=next_action, stop="step budget reached",
            ),
        )

    def _snapshot(
        self, completed: list[str], attempted_fixes: list[str],
        history: list[StepOutcome], steps_used: int, *,
        next_action: str, stop: str,
    ) -> ContinuationState:
        budget = {"steps_used": steps_used, "steps_limit": self.max_steps}
        budget.update(self.extra_budget)
        return ContinuationState(
            goal=self.goal,
            constraints=self.constraints,
            completed=completed,
            current_status=history[-1].observation if history else "no steps run yet",
            attempted_fixes=attempted_fixes,
            budget=budget,
            next_action=next_action,
            stop_condition=stop,
        )


def resume_loop(loop: BoundedTaskLoop, state: ContinuationState) -> LoopResult:
    """Resume a paused loop from its continuation state. Thin wrapper over
    :meth:`BoundedTaskLoop.run` so callers read intent clearly."""
    return loop.run(resume=state)
