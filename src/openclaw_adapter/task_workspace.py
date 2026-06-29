"""Typed task workspace for variable-bound tool pipelines (#53).

A WorkflowRunner executes a Workflow step-by-step, binding each step's output
to a named Variable in a VariableStore. Later steps resolve their inputs from
the store rather than receiving raw strings. WorkflowStore persists workflow
definitions and execution traces as JSON files.

Step kinds:
  tool_call     — calls a generated catalog tool with explicit params via the
                  ToolCallExecutor protocol (DynamicToolRunner.run_tool_step).
  command_sink  — calls a whitelisted slash command with a resolved variable value
                  via an injected CommandDispatcher.
  llm_transform — calls an LLM with a no-invention-constrained prompt; input
                  variable values are the sole grounding source.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
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
    # Set when the workflow fails structural validation before any step runs.
    # An empty step list alone does not mean failure; this field makes it explicit.
    validation_error: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.validation_error is None
            and all(st.status != "failed" for st in self.steps)
        )

    def to_dict(self) -> dict:
        d: dict = {
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
        if self.validation_error is not None:
            d["validation_error"] = self.validation_error
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "WorkflowTrace":
        variables = {
            k: Variable(
                name=v["name"], type=v["type"], value=v["value"],
                source_step=v["source_step"], provenance=v["provenance"],
            )
            for k, v in d.get("variables", {}).items()
        }
        steps = [
            StepTrace(
                step_id=s["step_id"], kind=s["kind"], status=s["status"],
                output_var=s.get("output_var"), error=s.get("error"),
                provenance=s.get("provenance"),
            )
            for s in d.get("steps", [])
        ]
        return cls(
            workflow_id=d["workflow_id"],
            goal=d["goal"],
            variables=variables,
            steps=steps,
            final_result=d.get("final_result"),
            validation_error=d.get("validation_error"),
        )


# ── Executor / LLM protocols ──────────────────────────────────────────────────

class ToolCallExecutor(Protocol):
    """Narrow interface the WorkflowRunner needs from DynamicToolRunner."""

    def run_tool_step(self, slug: str, explicit_params: dict) -> tuple[bool, str]:
        """Execute a catalog tool by slug with caller-supplied params.
        Returns ``(ok, result_text)`` — result_text is the answer on success
        or an error message on failure."""
        ...


class LLMClient(Protocol):
    """Minimal LLM interface needed for llm_transform steps."""

    def generate(self, prompt: str, *, temperature: float = 0.0) -> str: ...


# ── Runner ────────────────────────────────────────────────────────────────────

class WorkflowRunner:
    """Executes a Workflow sequentially, binding step outputs to Variables."""

    def __init__(
        self,
        executor: ToolCallExecutor,
        command_dispatcher: CommandDispatcher | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.executor = executor
        self.command_dispatcher: CommandDispatcher = command_dispatcher or {}
        self.llm_client = llm_client

    def run(self, workflow: Workflow) -> WorkflowTrace:
        """Execute all steps and return the full trace."""
        errors = workflow.validate_references()
        if errors:
            joined = "\n".join(errors)
            return WorkflowTrace(
                workflow_id=workflow.id,
                goal=workflow.goal,
                validation_error=joined,
                final_result=f"工作流定義有誤：\n{joined}",
            )

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
        self, step: WorkflowStep, store: VariableStore
    ) -> tuple[StepTrace, str | None]:
        if self.llm_client is None:
            return (
                StepTrace(
                    step_id=step.id, kind=step.kind, status="failed",
                    error="WorkflowRunner has no llm_client; cannot run llm_transform",
                ),
                None,
            )

        # Resolve and embed each input variable — these are the sole grounding source.
        input_blocks: list[str] = []
        for var_name in step.inputs:
            try:
                value = store.resolve(var_name)
            except KeyError as exc:
                return (
                    StepTrace(
                        step_id=step.id, kind=step.kind, status="failed",
                        error=str(exc),
                    ),
                    None,
                )
            input_blocks.append(f"[{var_name}]\n{value}")

        inputs_text = "\n\n".join(input_blocks)
        prompt = (
            f"以下は入力データです：\n\n{inputs_text}\n\n"
            f"タスク指示：{step.instructions or '内容を変換してください。'}\n\n"
            "厳守ルール（絶対に破らないこと）：\n"
            "- 上の「入力データ」にある内容だけを使うこと。\n"
            "- 入力データにない事実（温度、天気、地名、数値、出来事など）を"
            "捏造・推測・補完してはならない。\n"
            "- 結果だけを出力すること。説明・見出し・前置きは不要。"
        )

        try:
            result = self.llm_client.generate(prompt, temperature=0.7)
        except Exception as exc:
            logger.exception("task_workspace: llm_transform failed step=%s", step.id)
            return (
                StepTrace(
                    step_id=step.id, kind=step.kind, status="failed",
                    error=f"LLM transform 失敗: {exc}",
                ),
                None,
            )

        provenance = f"llm_transform(inputs={step.inputs})"
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


# ── Persistence ───────────────────────────────────────────────────────────────

class WorkflowStore:
    """Persists workflow definitions and execution traces as JSON files.

    Layout under ``base_dir``:
      <id>.json               — workflow definition
      traces/<id>/<ts_ms>.json — execution trace per run
    """

    def __init__(self, base_dir: Path) -> None:
        self._dir = Path(base_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / "traces").mkdir(exist_ok=True)

    # ── Workflow definitions ──────────────────────────────────────────────────

    def save(self, workflow: Workflow) -> None:
        (self._dir / f"{workflow.id}.json").write_text(
            json.dumps(workflow.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, workflow_id: str) -> Workflow | None:
        path = self._dir / f"{workflow_id}.json"
        if not path.exists():
            return None
        try:
            return Workflow.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            logger.exception("WorkflowStore: failed to load workflow %s", workflow_id)
            return None

    def list(self) -> list[Workflow]:
        workflows: list[Workflow] = []
        for p in sorted(self._dir.glob("*.json")):
            try:
                workflows.append(Workflow.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except Exception:
                logger.debug("WorkflowStore: skipping malformed file %s", p)
        return workflows

    def delete(self, workflow_id: str) -> bool:
        path = self._dir / f"{workflow_id}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    # ── Execution traces ──────────────────────────────────────────────────────

    def save_trace(self, trace: WorkflowTrace) -> None:
        traces_dir = self._dir / "traces" / trace.workflow_id
        traces_dir.mkdir(parents=True, exist_ok=True)
        ts = time.time_ns()
        (traces_dir / f"{ts}.json").write_text(
            json.dumps(trace.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_traces(self, workflow_id: str) -> list[WorkflowTrace]:
        traces_dir = self._dir / "traces" / workflow_id
        if not traces_dir.exists():
            return []
        traces: list[WorkflowTrace] = []
        for p in sorted(traces_dir.glob("*.json")):
            try:
                traces.append(WorkflowTrace.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except Exception:
                logger.debug("WorkflowStore: skipping malformed trace %s", p)
        return traces
