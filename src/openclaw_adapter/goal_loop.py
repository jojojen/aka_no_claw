"""Goal-level draft → run → replan loop for chat goals (#54 phase 3).

This module keeps the execution core testable and transport-agnostic. Live chat
can wire it in later without changing the planner or the typed workflow runner.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from .continuation_policy import operation_key
from .goal_planner import GoalPlanner
from .task_loop import BoundedTaskLoop, ContinuationState, LoopContext, LoopResult, StepOutcome
from .task_workspace import (
    Workflow,
    WorkflowRunner,
    WorkflowTrace,
    describe_workflow_step,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GoalLoopReport:
    done: bool
    final_result: str
    workflow: Workflow | None = None
    trace: WorkflowTrace | None = None
    continuation: "GoalLoopContinuation | None" = None
    replans_used: int = 0
    narration: tuple[str, ...] = ()


@dataclass(frozen=True)
class GoalLoopContinuation:
    state: ContinuationState
    workflow: Workflow | None = None
    trace: WorkflowTrace | None = None
    replans_used: int = 0
    narration: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        data: dict[str, object] = {
            "state": self.state.to_dict(),
            "replans_used": int(self.replans_used),
            "narration": list(self.narration),
        }
        if self.workflow is not None:
            data["workflow"] = self.workflow.to_dict()
        if self.trace is not None:
            data["trace"] = self.trace.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "GoalLoopContinuation":
        return cls(
            state=ContinuationState.from_dict(dict(data.get("state") or {})),
            workflow=(
                Workflow.from_dict(data["workflow"])
                if isinstance(data.get("workflow"), dict)
                else None
            ),
            trace=(
                WorkflowTrace.from_dict(data["trace"])
                if isinstance(data.get("trace"), dict)
                else None
            ),
            replans_used=int(data.get("replans_used") or 0),
            narration=tuple(str(x) for x in (data.get("narration") or [])),
        )


class GoalLoop:
    def __init__(
        self,
        *,
        goal: str,
        planner: GoalPlanner,
        executor,
        command_registry: dict[str, object],
        llm_client=None,
        trace_saver: Callable[[WorkflowTrace], None] | None = None,
        chat_id: str = "",
        max_steps: int = 6,
        replan_limit: int = 2,
        narrator: Callable[[str], None] | None = None,
        result_judge: Callable[[str, str], tuple[bool, str]] | None = None,
        seed_variables: dict[str, str] | None = None,
        seed_operations: dict[str, str] | None = None,
        conservative_synthesizer: Callable[[str, dict[str, str], str], str] | None = None,
    ) -> None:
        self.goal = goal
        self.planner = planner
        self.executor = executor
        self.command_registry = command_registry
        self.llm_client = llm_client or getattr(executor, "client", None)
        self.trace_saver = trace_saver
        self.chat_id = chat_id
        self.max_steps = max_steps
        self.replan_limit = replan_limit
        self.narrator = narrator
        self.result_judge = result_judge
        # Optional best-effort answer builder used ONLY when the loop exhausts
        # its replan budget with the goal still unmet. Given the goal, the
        # evidence gathered so far, and the last judge reason, it returns a
        # hedged answer that states what stayed unknown. When absent, the loop
        # keeps its previous behaviour (abort with the raw failure), so no
        # existing multi-step flow changes.
        self.conservative_synthesizer = conservative_synthesizer
        # Results that already exist before this run (e.g. a chat tool that just
        # completed, or succeeded steps from a previous workflow attempt). They
        # are pre-bound in the runner and shown to the planner so plans consume
        # them instead of re-executing expensive steps.
        self.seed_variables = dict(seed_variables or {})
        # Operations (``operation_key`` -> produced result) that already ran
        # before or during this goal — most importantly the chat tool whose
        # incomplete result triggered the escalation. The command dispatcher
        # refuses to re-run any command whose (command, input) collapses to a
        # key already here, returning the prior artifact instead. This makes
        # "at most one expensive run per canonical operation" a deterministic
        # guarantee (issue #81), not a hope pinned on the planner prompt or the
        # LLM satisfaction judge. Purely structural — no domain rules.
        self.seed_operations = dict(seed_operations or {})

    def run(self, resume: GoalLoopContinuation | None = None) -> GoalLoopReport:
        scratch = {
            "workflow": resume.workflow if resume is not None else None,
            "trace": resume.trace if resume is not None else None,
            "replans_used": int(resume.replans_used) if resume is not None else 0,
            "needs_replan": False,
            "pause_reason": None,
            "abort": None,
            # Set when the replan budget is spent with the goal still unmet;
            # distinguishes that terminal case from other aborts so only it
            # gets conservative synthesis.
            "exhausted": False,
            # Last judge reason for "goal not achieved", forwarded to the
            # conservative synthesizer so it can name what stayed unknown.
            "last_reason": "",
            "narration": list(resume.narration) if resume is not None else [],
            "seeds": dict(self.seed_variables),
            # Static type (plain_text/speech_text/command_result/...) each seed
            # had when it was first produced, so a replan carrying it forward
            # doesn't lose that information (see WorkflowRunner.run()).
            "seed_types": {},
            # Shared across draft + every replan of this run so a command that
            # already ran (with the same normalised input) is never executed a
            # second time — the dispatcher returns the cached artifact instead.
            "executed_operations": dict(self.seed_operations),
        }
        if resume is not None and resume.trace is not None:
            # A resumed run may go straight to replan; make the previous
            # attempt's bound variables reusable there too.
            scratch["seeds"].update(
                {name: var.value for name, var in resume.trace.variables.items()}
            )
            scratch["seed_types"].update(
                {name: var.type for name, var in resume.trace.variables.items()}
            )
        for line in scratch["narration"]:
            logger.info("[goal-loop] %s", line)
        if not scratch["narration"]:
            self._narrate(scratch, f"已理解目標為：{self.goal}")
        if scratch["trace"] is not None and not scratch["trace"].ok:
            scratch["needs_replan"] = True
        loop = BoundedTaskLoop(
            self.goal,
            steps=self._steps(scratch),
            decider=self._decider(scratch),
            max_steps=self.max_steps,
            constraints=f"replan at most {self.replan_limit} time(s); use registered commands only",
        )
        result = loop.run(resume=resume.state if resume is not None else None)
        trace = scratch["trace"]
        workflow = scratch["workflow"]
        narration = tuple(scratch["narration"])

        if scratch["abort"]:
            if scratch.get("exhausted") and self.conservative_synthesizer is not None:
                synthesized = self._synthesize_conservative(scratch)
                if synthesized:
                    self._narrate(
                        scratch,
                        "已用盡重規劃額度，改以現有證據給出保守結論（明示仍缺的部分）。",
                    )
                    return GoalLoopReport(
                        done=True,
                        final_result=synthesized,
                        workflow=workflow,
                        trace=trace,
                        replans_used=int(scratch["replans_used"]),
                        narration=tuple(scratch["narration"]),
                    )
            return GoalLoopReport(
                done=False,
                final_result=str(scratch["abort"]),
                workflow=workflow,
                trace=trace,
                continuation=self._build_continuation(
                    result=result,
                    workflow=workflow,
                    trace=trace,
                    replans_used=int(scratch["replans_used"]),
                    replan_limit=self.replan_limit,
                    narration=narration,
                ),
                replans_used=int(scratch["replans_used"]),
                narration=narration,
            )
        if scratch["pause_reason"]:
            final_result = (
                trace.final_result
                if trace is not None and trace.final_result
                else str(scratch["pause_reason"])
            )
            return GoalLoopReport(
                done=False,
                final_result=final_result,
                workflow=workflow,
                trace=trace,
                continuation=self._build_continuation(
                    result=result,
                    workflow=workflow,
                    trace=trace,
                    replans_used=int(scratch["replans_used"]),
                    replan_limit=self.replan_limit,
                    narration=narration,
                    next_action_override="run_workflow",
                    stop_condition_override=str(scratch["pause_reason"]),
                ),
                replans_used=int(scratch["replans_used"]),
                narration=narration,
            )
        if result.done and trace is not None and trace.ok:
            return GoalLoopReport(
                done=True,
                final_result=trace.final_result or "（無輸出）",
                workflow=workflow,
                trace=trace,
                replans_used=int(scratch["replans_used"]),
                narration=narration,
            )
        final_result = (
            trace.final_result if trace is not None and trace.final_result
            else (result.stop_condition or "goal loop incomplete")
        )
        return GoalLoopReport(
            done=False,
            final_result=final_result,
            workflow=workflow,
            trace=trace,
            continuation=self._build_continuation(
                result=result,
                workflow=workflow,
                trace=trace,
                replans_used=int(scratch["replans_used"]),
                replan_limit=self.replan_limit,
                narration=narration,
            ),
            replans_used=int(scratch["replans_used"]),
            narration=narration,
        )

    @staticmethod
    def _build_continuation(
        *,
        result: LoopResult,
        workflow: Workflow | None,
        trace: WorkflowTrace | None,
        replans_used: int,
        replan_limit: int,
        narration: tuple[str, ...],
        next_action_override: str | None = None,
        stop_condition_override: str | None = None,
    ) -> GoalLoopContinuation | None:
        if result.state is None:
            return None
        state = result.state
        if next_action_override is not None or stop_condition_override is not None:
            state = ContinuationState(
                goal=state.goal,
                constraints=state.constraints,
                completed=list(state.completed),
                current_status=state.current_status,
                attempted_fixes=list(state.attempted_fixes),
                budget=dict(state.budget),
                next_action=next_action_override if next_action_override is not None else state.next_action,
                stop_condition=(
                    stop_condition_override if stop_condition_override is not None else state.stop_condition
                ),
            )
        if trace is not None and trace.budget_exhausted is not None:
            budget = dict(state.budget)
            budget["search_used"] = trace.budget_exhausted.used
            budget["search_limit"] = trace.budget_exhausted.limit
            if trace.budget_exhausted.hard_limit is not None:
                budget["search_hard_limit"] = trace.budget_exhausted.hard_limit
            budget["search_granted_extra"] = trace.budget_exhausted.granted_extra
            state = ContinuationState(
                goal=state.goal,
                constraints=state.constraints,
                completed=list(state.completed),
                current_status=state.current_status,
                attempted_fixes=list(state.attempted_fixes),
                budget=budget,
                next_action=state.next_action,
                stop_condition=state.stop_condition,
            )
        return GoalLoopContinuation(
            state=GoalLoop._state_with_replan_budget(
                state,
                replans_used=replans_used,
                replan_limit=replan_limit,
            ),
            workflow=workflow,
            trace=trace,
            replans_used=replans_used,
            narration=narration,
        )

    @staticmethod
    def _state_with_replan_budget(
        state: ContinuationState,
        *,
        replans_used: int,
        replan_limit: int | None,
    ) -> ContinuationState:
        budget = dict(state.budget)
        budget["replans_used"] = replans_used
        if replan_limit is not None:
            budget["replans_limit"] = replan_limit
        return ContinuationState(
            goal=state.goal,
            constraints=state.constraints,
            completed=list(state.completed),
            current_status=state.current_status,
            attempted_fixes=list(state.attempted_fixes),
            budget=budget,
            next_action=state.next_action,
            stop_condition=state.stop_condition,
        )

    def _steps(self, scratch: dict) -> dict[str, Callable[[LoopContext], StepOutcome]]:
        def draft(ctx: LoopContext) -> StepOutcome:
            self._narrate(scratch, "規劃任務工作流草稿…")
            workflow, error, _used_fallback = self._planner_draft(scratch)
            if workflow is None:
                scratch["abort"] = error or "無法產生工作流草稿"
                self._narrate(scratch, f"草稿失敗：{scratch['abort']}")
                return StepOutcome(observation="draft failed", failed=True, reflection=scratch["abort"])
            scratch["workflow"] = workflow
            self._narrate(scratch, f"草稿完成：{workflow.id}（{len(workflow.steps)} 步）")
            self._narrate_workflow_steps(scratch, workflow)
            return StepOutcome(observation=f"drafted {workflow.id} with {len(workflow.steps)} step(s)")

        def run_workflow(ctx: LoopContext) -> StepOutcome:
            workflow = scratch.get("workflow")
            if workflow is None:
                scratch["abort"] = "沒有可執行的工作流草稿"
                return StepOutcome(observation="no workflow", failed=True, reflection=scratch["abort"])
            self._narrate(scratch, f"開始執行工作流：{workflow.id}")
            trace = self._build_runner(
                step_observer=lambda line: self._narrate(scratch, line),
                seed_variables=scratch["seeds"],
                seed_variable_types=scratch["seed_types"],
                executed_operations=scratch["executed_operations"],
                narrate=lambda line: self._narrate(scratch, line),
            ).run(workflow)
            scratch["trace"] = trace
            # Carry every bound variable (succeeded steps + prior seeds) forward
            # so a replan can consume them instead of re-running those steps.
            scratch["seeds"].update(
                {name: var.value for name, var in trace.variables.items()}
            )
            scratch["seed_types"].update(
                {name: var.type for name, var in trace.variables.items()}
            )
            scratch["needs_replan"] = False
            scratch["pause_reason"] = None
            if trace.ok:
                # Every step succeeding is not the same as the goal being
                # achieved (e.g. a command replying with a follow-up question).
                # Judge the final result against the goal; if it falls short,
                # feed the reason back into replan instead of declaring done.
                achieved, reason = self._judge_result(scratch, trace.final_result or "")
                if not achieved:
                    scratch["needs_replan"] = True
                    scratch["last_reason"] = reason or scratch.get("last_reason") or ""
                    self._narrate(
                        scratch,
                        f"結果未達成目標：{reason or '（無說明）'}，準備重新規劃",
                    )
                    trace.narration = list(scratch["narration"])
                    if self.trace_saver is not None:
                        try:
                            self.trace_saver(trace)
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "goal_loop: failed to save trace workflow=%s", workflow.id
                            )
                    return StepOutcome(
                        observation=trace.final_result or "workflow result did not achieve goal",
                        failed=True,
                        reflection=reason or "workflow completed but goal not achieved",
                    )
                self._narrate(scratch, f"工作流完成：{trace.final_result or '（無輸出）'}")
                trace.narration = list(scratch["narration"])
                if self.trace_saver is not None:
                    try:
                        self.trace_saver(trace)
                    except Exception:  # noqa: BLE001
                        logger.exception("goal_loop: failed to save trace workflow=%s", workflow.id)
                return StepOutcome(observation=trace.final_result or "workflow completed", done=True)
            if trace.budget_exhausted is not None:
                scratch["pause_reason"] = self._budget_pause_reason(trace.budget_exhausted)
                self._narrate(scratch, f"工作流暫停：{trace.final_result or scratch['pause_reason']}")
                trace.narration = list(scratch["narration"])
                if self.trace_saver is not None:
                    try:
                        self.trace_saver(trace)
                    except Exception:  # noqa: BLE001
                        logger.exception("goal_loop: failed to save trace workflow=%s", workflow.id)
                return StepOutcome(observation=trace.final_result or str(scratch["pause_reason"]))
            scratch["needs_replan"] = True
            self._narrate(scratch, f"工作流失敗：{trace.final_result or '（無詳情）'}")
            trace.narration = list(scratch["narration"])
            if self.trace_saver is not None:
                try:
                    self.trace_saver(trace)
                except Exception:  # noqa: BLE001
                    logger.exception("goal_loop: failed to save trace workflow=%s", workflow.id)
            return StepOutcome(
                observation=trace.final_result or "workflow failed",
                failed=True,
                reflection=trace.validation_error or trace.final_result or "workflow failed",
            )

        def replan(ctx: LoopContext) -> StepOutcome:
            workflow = scratch.get("workflow")
            trace = scratch.get("trace")
            if workflow is None or trace is None:
                scratch["abort"] = "缺少重規劃所需的 workflow 或 trace"
                return StepOutcome(observation="replan unavailable", failed=True, reflection=scratch["abort"])
            self._narrate(scratch, f"進入重規劃（{scratch['replans_used'] + 1}/{self.replan_limit}）")
            revised, error, _used_fallback = self._planner_replan(scratch, workflow, trace)
            if revised is None:
                scratch["abort"] = error or "重規劃失敗"
                self._narrate(scratch, f"重規劃失敗：{scratch['abort']}")
                return StepOutcome(observation="replan failed", failed=True, reflection=scratch["abort"])
            scratch["workflow"] = revised
            scratch["replans_used"] += 1
            scratch["needs_replan"] = False
            # drop the pre-replan trace: it may be ok=True (judged not achieving
            # the goal), and a stale ok trace would stop the decider before the
            # revised workflow gets its run
            scratch["trace"] = None
            self._narrate(scratch, f"重規劃完成：{revised.id}（{len(revised.steps)} 步）")
            self._narrate_workflow_steps(scratch, revised)
            return StepOutcome(observation=f"replanned to {revised.id}")

        return {
            "draft": draft,
            "run_workflow": run_workflow,
            "replan": replan,
        }

    def _decider(self, scratch: dict):
        def decide(ctx: LoopContext) -> str:
            if scratch.get("abort"):
                return ""
            if scratch.get("pause_reason"):
                return ""
            done = {c.split(":", 1)[0] for c in ctx.completed}
            if "draft" not in done:
                return "draft"
            trace = scratch.get("trace")
            if trace is None:
                return "run_workflow"
            # needs_replan can be true even when trace.ok (goal-achievement
            # judge rejected the result), so it must be checked first.
            if scratch["needs_replan"] and scratch["replans_used"] >= self.replan_limit:
                scratch["abort"] = trace.final_result or "replan limit reached"
                scratch["exhausted"] = True
                return ""
            if scratch["needs_replan"]:
                return "replan"
            if trace.ok:
                return ""
            return "run_workflow"
        return decide

    def _planner_draft(self, scratch: dict):
        seeds = dict(scratch.get("seeds") or {})
        return self.planner.draft(self.goal, seed_variables=seeds)

    def _planner_replan(self, scratch: dict, workflow: Workflow, trace: WorkflowTrace):
        seeds = dict(scratch.get("seeds") or {})
        return self.planner.replan(self.goal, workflow, trace, seed_variables=seeds)

    def _build_runner(
        self,
        step_observer: Callable[[str], None] | None = None,
        seed_variables: dict[str, str] | None = None,
        seed_variable_types: dict[str, str] | None = None,
        executed_operations: dict[str, str] | None = None,
        narrate: Callable[[str], None] | None = None,
    ) -> WorkflowRunner:
        return WorkflowRunner(
            executor=self.executor,
            command_dispatcher=_command_dispatcher_for_chat(
                self.command_registry,
                chat_id=self.chat_id,
                executed_operations=executed_operations,
                narrate=narrate,
            ),
            llm_client=self.llm_client,
            step_observer=step_observer,
            seed_variables=seed_variables,
            seed_variable_types=seed_variable_types,
        )

    def _judge_result(self, scratch: dict, final_result: str) -> tuple[bool, str]:
        """LLM judgment of whether ``final_result`` achieves the goal. No
        keyword heuristics — open-world adequacy is the judge's call. A judge
        failure must not sink an otherwise successful run, so it counts as
        achieved."""
        if self.result_judge is None:
            return True, ""
        self._narrate(scratch, "檢查結果是否達成目標…")
        try:
            achieved, reason = self.result_judge(self.goal, final_result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("goal_loop: result judge failed")
            self._narrate(scratch, f"結果檢查不可用（{exc}），視為完成")
            return True, ""
        return bool(achieved), str(reason or "")

    def _synthesize_conservative(self, scratch: dict) -> str:
        """Best-effort answer when the replan budget is spent and the goal is
        still unmet. Hands the gathered evidence + last judge reason to the
        injected synthesizer; a failure here just falls back to the raw abort."""
        if self.conservative_synthesizer is None:
            return ""
        seeds = {
            name: str(value)
            for name, value in (scratch.get("seeds") or {}).items()
            if str(value).strip()
        }
        if not seeds:
            return ""
        self._narrate(scratch, "以現有證據合成保守結論…")
        try:
            answer = self.conservative_synthesizer(
                self.goal, seeds, str(scratch.get("last_reason") or "")
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("goal_loop: conservative synthesizer failed")
            self._narrate(scratch, f"保守合成不可用（{exc}）")
            return ""
        return str(answer or "").strip()

    def _narrate(self, scratch: dict, line: str) -> None:
        scratch["narration"].append(line)
        logger.info("[goal-loop] %s", line)
        if self.narrator is not None:
            try:
                self.narrator(line)
            except Exception:  # noqa: BLE001
                logger.exception("goal_loop: narrator callback failed")

    def _narrate_workflow_steps(self, scratch: dict, workflow: Workflow) -> None:
        self._narrate(scratch, "子任務：")
        for index, step in enumerate(workflow.steps, 1):
            self._narrate(scratch, f"{index}. {describe_workflow_step(step)}")

    @staticmethod
    def _budget_pause_reason(budget) -> str:
        if getattr(budget, "kind", "") == "search":
            used = int(getattr(budget, "used", 0) or 0)
            limit = int(getattr(budget, "limit", 0) or 0)
            hard_limit = int(getattr(budget, "hard_limit", 0) or 0)
            if hard_limit and used >= hard_limit:
                return f"search hard cap reached ({used}/{hard_limit})"
            return f"search soft cap reached ({used}/{limit})"
        return "budget exhausted"


def _command_dispatcher_for_chat(
    command_registry: dict[str, object],
    *,
    chat_id: str,
    executed_operations: dict[str, str] | None = None,
    narrate: Callable[[str], None] | None = None,
) -> dict[str, Callable[[str], str]]:
    # ``executed_operations`` is the run-scoped dedup memo (operation_key ->
    # result). When present, a command whose (command, input) collapses to a
    # key already run is NOT executed again — its prior artifact is returned,
    # so an expensive operation like /research runs at most once per canonical
    # request even across replans (issue #81). When absent, behaviour is the
    # plain pass-through (used by callers that don't want dedup, e.g. tests).
    memo = executed_operations

    def _announce_reuse(command: str) -> None:
        if narrate is None:
            return
        try:
            narrate(f"略過重複操作：{command}（沿用先前結果）")
        except Exception:  # noqa: BLE001
            logger.exception("goal_loop: dedup narrate callback failed")

    dispatcher: dict[str, Callable[[str], str]] = {}
    for command, registered in command_registry.items():
        handler = getattr(registered, "handler", None)
        if handler is None:
            continue

        def _dispatch(text: str, handler=handler, chat_id=chat_id, command=command) -> str:
            if memo is not None:
                key = operation_key(command, text)
                if key in memo:
                    _announce_reuse(command)
                    return memo[key]
                result = handler(text, chat_id)
                answer = str(result[0] if isinstance(result, tuple) else result)
                memo[key] = answer
                return answer
            result = handler(text, chat_id)
            return str(result[0] if isinstance(result, tuple) else result)

        dispatcher[command] = _dispatch
    return dispatcher
