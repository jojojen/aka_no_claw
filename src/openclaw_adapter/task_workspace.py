"""Typed task workspace for variable-bound tool pipelines (#53, Phase 1).

A WorkflowRunner executes a Workflow step-by-step, binding each step's output
to a named Variable in a VariableStore. Later steps resolve their inputs from
the store rather than receiving raw strings. The runner is pure logic: no
Telegram, no persistence, no LLM calls in this phase.

Step kinds supported in Phase 1:
  tool_call     — calls a generated catalog tool with explicit params via the
                  ToolCallExecutor protocol (DynamicToolRunner.run_tool_step).
  command_sink  — calls a whitelisted slash command with a resolved variable value
                  via an injected CommandDispatcher.
  llm_transform — not yet implemented (Phase 4); immediately returns failed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol

logger = logging.getLogger(__name__)

# Only explicitly allowlisted commands may be used as sinks (#53 §E).
COMMAND_SINK_ALLOWLIST: frozenset[str] = frozenset({"/saynow"})

# Maps slash-command string to a callable that takes the input text and returns
# the result string. Callers inject this into WorkflowRunner.
CommandDispatcher = dict[str, Callable[[str], str]]


# ── Schema ───────────────────────────────────────────────────────────────────

@dataclass
class WorkflowStep:
    id: str
    kind: Literal["tool_call", "command_sink", "llm_transform"]
    output: str  # name of the variable this step writes

    # tool_call
    tool: str | None = None          # catalog slug, e.g. "city_weather_abc123"
    args: dict = field(default_factory=dict)  # explicit params; "$varname" refs resolved

    # llm_transform (Phase 4)
    inputs: list[str] = field(default_factory=list)
    instructions: str | None = None

    # command_sink
    command: str | None = None       # must be in COMMAND_SINK_ALLOWLIST
    input: str | None = None         # single input variable name

    def to_dict(self) -> dict:
        d: dict = {"id": self.id, "kind": self.kind, "output": self.output}
        if self.tool is not None:
            d["tool"] = self.tool
        if self.args:
            d["args"] = self.args
        if self.inputs:
            d["inputs"] = self.inputs
        if self.instructions is not None:
            d["instructions"] = self.instructions
        if self.command is not None:
            d["command"] = self.command
        if self.input is not None:
            d["input"] = self.input
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "WorkflowStep":
        return cls(
            id=d["id"],
            kind=d["kind"],
            output=d["output"],
            tool=d.get("tool"),
            args=d.get("args") or {},
            inputs=d.get("inputs") or [],
            instructions=d.get("instructions"),
            command=d.get("command"),
            input=d.get("input"),
        )


@dataclass
class Workflow:
    id: str
    goal: str
    steps: list[WorkflowStep] = field(default_factory=list)

    def validate_references(self) -> list[str]:
        """Return a list of structural errors (forward refs, unlisted commands).
        An empty list means the workflow is structurally sound."""
        errors: list[str] = []
        defined: set[str] = set()
        for step in self.steps:
            if step.kind == "tool_call":
                if not step.tool:
                    errors.append(f"Step {step.id}: tool_call is missing 'tool'")
                for k, v in step.args.items():
                    if isinstance(v, str) and v.startswith("$"):
                        ref = v[1:]
                        if ref not in defined:
                            errors.append(
                                f"Step {step.id}: arg '{k}' references undefined variable '{ref}'"
                            )
            elif step.kind == "command_sink":
                if not step.command:
                    errors.append(f"Step {step.id}: command_sink is missing 'command'")
                elif step.command not in COMMAND_SINK_ALLOWLIST:
                    errors.append(
                        f"Step {step.id}: command '{step.command}' is not in the "
                        f"allowlist {sorted(COMMAND_SINK_ALLOWLIST)}"
                    )
                if step.input and step.input not in defined:
                    errors.append(
                        f"Step {step.id}: input '{step.input}' is not yet produced "
                        f"by a prior step"
                    )
            elif step.kind == "llm_transform":
                for var in step.inputs:
                    if var not in defined:
                        errors.append(
                            f"Step {step.id}: input '{var}' is not yet produced "
                            f"by a prior step"
                        )
            defined.add(step.output)
        return errors

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Workflow":
        return cls(
            id=d["id"],
            goal=d["goal"],
            steps=[WorkflowStep.from_dict(s) for s in d.get("steps", [])],
        )


# ── Variable store ────────────────────────────────────────────────────────────

@dataclass
class Variable:
    name: str
    type: str
    value: str
    source_step: str
    provenance: str


class VariableStore:
    """Holds the runtime variable table for a single workflow execution."""

    def __init__(self) -> None:
        self._vars: dict[str, Variable] = {}

    def bind(
        self,
        name: str,
        value: str,
        source_step: str,
        provenance: str,
        type_: str = "text",
    ) -> Variable:
        var = Variable(
            name=name,
            type=type_,
            value=value,
            source_step=source_step,
            provenance=provenance,
        )
        self._vars[name] = var
        return var

    def resolve(self, name: str) -> str:
        """Return the value of a variable. Raises KeyError if not yet defined."""
        if name not in self._vars:
            raise KeyError(f"Variable '{name}' not found in workspace")
        return self._vars[name].value

    def get(self, name: str) -> Variable | None:
        return self._vars.get(name)

    def snapshot(self) -> dict[str, Variable]:
        return dict(self._vars)


# ── Trace ─────────────────────────────────────────────────────────────────────

@dataclass
class StepTrace:
    step_id: str
    kind: str
    status: Literal["ok", "failed", "skipped"]
    output_var: str | None = None
    error: str | None = None
    provenance: str | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "step_id": self.step_id,
            "kind": self.kind,
            "status": self.status,
        }
        if self.output_var is not None:
            d["output_var"] = self.output_var
        if self.error is not None:
            d["error"] = self.error
        if self.provenance is not None:
            d["provenance"] = self.provenance
        return d


@dataclass
class WorkflowTrace:
    workflow_id: str
    goal: str
    variables: dict[str, Variable] = field(default_factory=dict)
    steps: list[StepTrace] = field(default_factory=list)
    final_result: str | None = None

    @property
    def ok(self) -> bool:
        return all(st.status != "failed" for st in self.steps)

    def to_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "goal": self.goal,
            "variables": {
                k: {
                    "name": v.name,
                    "type": v.type,
                    "value": v.value,
                    "source_step": v.source_step,
                    "provenance": v.provenance,
                }
                for k, v in self.variables.items()
            },
            "steps": [s.to_dict() for s in self.steps],
            "final_result": self.final_result,
        }


# ── Executor protocol ─────────────────────────────────────────────────────────

class ToolCallExecutor(Protocol):
    """Narrow interface the WorkflowRunner needs from DynamicToolRunner."""

    def run_tool_step(self, slug: str, explicit_params: dict) -> tuple[bool, str]:
        """Execute a catalog tool by slug with caller-supplied params.
        Returns ``(ok, result_text)`` — result_text is the answer on success
        or an error message on failure."""
        ...


# ── Runner ────────────────────────────────────────────────────────────────────

class WorkflowRunner:
    """Executes a Workflow sequentially, binding step outputs to Variables."""

    def __init__(
        self,
        executor: ToolCallExecutor,
        command_dispatcher: CommandDispatcher | None = None,
    ) -> None:
        self.executor = executor
        self.command_dispatcher: CommandDispatcher = command_dispatcher or {}

    def run(self, workflow: Workflow) -> WorkflowTrace:
        """Execute all steps and return the full trace."""
        errors = workflow.validate_references()
        if errors:
            trace = WorkflowTrace(workflow_id=workflow.id, goal=workflow.goal)
            trace.final_result = "工作流定義有誤：\n" + "\n".join(errors)
            return trace

        store = VariableStore()
        trace = WorkflowTrace(workflow_id=workflow.id, goal=workflow.goal)
        failed = False
        last_output_var: str | None = None

        for step in workflow.steps:
            if failed:
                trace.steps.append(
                    StepTrace(step_id=step.id, kind=step.kind, status="skipped")
                )
                continue

            step_trace, produced_var = self._run_step(step, store)
            trace.steps.append(step_trace)

            if step_trace.status == "failed":
                failed = True
            elif produced_var:
                last_output_var = produced_var

        trace.variables = store.snapshot()

        if not failed and last_output_var:
            trace.final_result = store.resolve(last_output_var)
        elif failed:
            for st in trace.steps:
                if st.status == "failed" and st.error:
                    trace.final_result = f"工作流在步驟 {st.step_id} 失敗：{st.error}"
                    break

        return trace

    # ── Step dispatch ─────────────────────────────────────────────────────────

    def _run_step(
        self, step: WorkflowStep, store: VariableStore
    ) -> tuple[StepTrace, str | None]:
        if step.kind == "tool_call":
            return self._run_tool_call(step, store)
        if step.kind == "command_sink":
            return self._run_command_sink(step, store)
        if step.kind == "llm_transform":
            return self._run_llm_transform(step, store)
        st = StepTrace(
            step_id=step.id, kind=step.kind, status="failed",
            error=f"未知 step kind: {step.kind!r}",
        )
        return st, None

    def _run_tool_call(
        self, step: WorkflowStep, store: VariableStore
    ) -> tuple[StepTrace, str | None]:
        # Resolve $variable references in args before execution.
        resolved_args: dict = {}
        for k, v in step.args.items():
            if isinstance(v, str) and v.startswith("$"):
                ref = v[1:]
                try:
                    resolved_args[k] = store.resolve(ref)
                except KeyError as exc:
                    return (
                        StepTrace(
                            step_id=step.id, kind=step.kind, status="failed",
                            error=str(exc),
                        ),
                        None,
                    )
            else:
                resolved_args[k] = v

        slug = step.tool or ""
        ok, result_text = self.executor.run_tool_step(slug, resolved_args)

        if ok:
            provenance = f"{slug}({resolved_args})"
            var = store.bind(
                step.output, result_text,
                source_step=step.id,
                provenance=provenance,
            )
            return (
                StepTrace(
                    step_id=step.id, kind=step.kind, status="ok",
                    output_var=step.output, provenance=var.provenance,
                ),
                step.output,
            )

        return (
            StepTrace(
                step_id=step.id, kind=step.kind, status="failed",
                output_var=step.output, error=result_text,
            ),
            None,
        )

    def _run_command_sink(
        self, step: WorkflowStep, store: VariableStore
    ) -> tuple[StepTrace, str | None]:
        if step.command not in COMMAND_SINK_ALLOWLIST:
            return (
                StepTrace(
                    step_id=step.id, kind=step.kind, status="failed",
                    error=f"command '{step.command}' is not in the allowlist",
                ),
                None,
            )

        handler = self.command_dispatcher.get(step.command or "")
        if handler is None:
            return (
                StepTrace(
                    step_id=step.id, kind=step.kind, status="failed",
                    error=f"no handler registered for '{step.command}'",
                ),
                None,
            )

        try:
            input_value = store.resolve(step.input or "")
        except KeyError as exc:
            return (
                StepTrace(
                    step_id=step.id, kind=step.kind, status="failed",
                    error=str(exc),
                ),
                None,
            )

        try:
            result = handler(input_value)
        except Exception as exc:
            logger.exception("task_workspace: command sink %s raised", step.command)
            return (
                StepTrace(
                    step_id=step.id, kind=step.kind, status="failed",
                    error=f"{step.command} failed: {exc}",
                ),
                None,
            )

        provenance = f"{step.command}(input={step.input})"
        var = store.bind(
            step.output, result,
            source_step=step.id,
            provenance=provenance,
        )
        return (
            StepTrace(
                step_id=step.id, kind=step.kind, status="ok",
                output_var=step.output, provenance=var.provenance,
            ),
            step.output,
        )

    def _run_llm_transform(
        self, step: WorkflowStep, store: VariableStore  # noqa: ARG002
    ) -> tuple[StepTrace, str | None]:
        # Phase 4 — not yet implemented.
        return (
            StepTrace(
                step_id=step.id, kind=step.kind, status="failed",
                error="llm_transform は Phase 4 で実装予定です",
            ),
            None,
        )
