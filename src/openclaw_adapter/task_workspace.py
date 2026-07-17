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
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal, Protocol

logger = logging.getLogger(__name__)

# Only explicitly allowlisted commands may be used as sinks (#53 §E).
# Commands that are NEVER allowed as workflow command sinks, even if registered.
# Policy: any command present in the runtime command registry is schedulable as a
# workflow sink UNLESS it appears in this denylist.  This mirrors /schedulehome
# semantics so the two execution layers stay in sync automatically.
COMMAND_SINK_DENYLIST: frozenset[str] = frozenset({
    # service / system destructive
    "/restartall",
    # arbitrary code execution
    "/new",
    # filesystem / backup operations
    "/backupclaw", "/backup", "/clawrecover", "/recoverclaw",
    # meta / recursive scheduling
    "/schedulehome", "/workflow",
    # image-only commands are registered for Telegram dispatch but cannot run as
    # text-only workflow sinks.
    "/scan", "/image", "/photo",
    # monitoring config mutations (SNS write-side)
    "/snsadd", "/sns_add", "/snsdelete", "/sns_delete", "/snsclearfilter",
    # shell-like commands if ever registered
    "/bash", "/exec", "/rm", "/shell",
})

# Keep the old name as a read-only alias so any remaining callsite that imports
# COMMAND_SINK_ALLOWLIST still compiles.  Prefer COMMAND_SINK_DENYLIST for new code.
# NOTE: this is intentionally empty — use is_command_sink_allowed() for policy checks.
COMMAND_SINK_ALLOWLIST: frozenset[str] = frozenset()  # deprecated; use COMMAND_SINK_DENYLIST


def is_command_sink_allowed(command: str) -> bool:
    """Return True if ``command`` may be used as a workflow command sink.

    A command is allowed when it is NOT in the explicit denylist.  The caller is
    responsible for confirming that a handler actually exists at runtime."""
    return bool(command) and command not in COMMAND_SINK_DENYLIST

# Variable type tags understood by the runtime.
VARIABLE_TYPE_PLAIN_TEXT = "plain_text"
VARIABLE_TYPE_SPEECH_TEXT = "speech_text"
VARIABLE_TYPE_COMMAND_RESULT = "command_result"
VARIABLE_TYPE_EVIDENCE = "evidence"
VARIABLE_TYPE_USER_CONTEXT = "user_context"
VARIABLE_TYPE_UNTRUSTED_CONTEXT = "untrusted_context"
VARIABLE_TYPE_REQUIREMENT = "requirement"

_TRUSTED_FACT_TYPES = frozenset({
    VARIABLE_TYPE_EVIDENCE,
    VARIABLE_TYPE_USER_CONTEXT,
    VARIABLE_TYPE_COMMAND_RESULT,
})
_NUMERIC_ATOM_RE = re.compile(
    r"(?<![\w])(?:[¥$€£]\s*)?\d[\d,]*(?:\.\d+)?%?(?![\w])"
)


def _normalise_numeric_atom(value: str) -> str:
    return re.sub(r"[\s,]", "", value).lower()


def unsupported_numeric_atoms(answer: str, trusted_inputs: str) -> list[str]:
    """Return material numeric claims absent from factual/user evidence.

    Small bare integers are commonly list numbering, so only currency,
    percentages, decimals, or magnitudes >= 100 are gated.  This is a generic
    provenance check: no product, price, marketplace, or grading vocabulary is
    involved.
    """
    allowed = {
        _normalise_numeric_atom(atom)
        for atom in _NUMERIC_ATOM_RE.findall(trusted_inputs)
    }
    unsupported: list[str] = []
    for match in _NUMERIC_ATOM_RE.finditer(answer):
        atom = match.group(0)
        normalized = _normalise_numeric_atom(atom)
        digits = re.sub(r"\D", "", normalized)
        line_prefix = answer[answer.rfind("\n", 0, match.start()) + 1 : match.start()]
        suffix = answer[match.end() : match.end() + 1]
        is_list_marker = not line_prefix.strip() and suffix in {".", "、", ")", "）"}
        material = (
            any(symbol in atom for symbol in "¥$€£%")
            or "." in atom
            or (digits.isdigit() and int(digits) >= 100)
            or not is_list_marker
        )
        if material and normalized not in allowed and atom not in unsupported:
            unsupported.append(atom)
    return unsupported

# Accepted input variable types per command sink.
# None means any text type is accepted (generic text-input commands).
# Commands that are TTS-focused require plain or speech text to avoid passing
# raw command-result objects (e.g. JSON) to a voice synthesiser.
COMMAND_SINK_INPUT_TYPES: dict[str, frozenset[str] | None] = {
    "/saynow": frozenset({
        VARIABLE_TYPE_PLAIN_TEXT, VARIABLE_TYPE_SPEECH_TEXT, VARIABLE_TYPE_EVIDENCE,
    }),
    "/generateaudio": frozenset({
        VARIABLE_TYPE_PLAIN_TEXT, VARIABLE_TYPE_SPEECH_TEXT, VARIABLE_TYPE_EVIDENCE,
    }),
}

# Maps slash-command string to a callable that takes the input text and returns
# the result string. Callers inject this into WorkflowRunner.
CommandDispatcher = dict[str, Callable[[str], str]]


@dataclass(frozen=True)
class WorkflowBudgetExhausted:
    kind: str
    used: int
    limit: int
    hard_limit: int | None = None
    granted_extra: int = 0

    def to_dict(self) -> dict:
        out = {
            "kind": self.kind,
            "used": self.used,
            "limit": self.limit,
            "granted_extra": self.granted_extra,
        }
        if self.hard_limit is not None:
            out["hard_limit"] = self.hard_limit
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowBudgetExhausted":
        hard_limit = data.get("hard_limit")
        return cls(
            kind=str(data.get("kind") or ""),
            used=int(data.get("used") or 0),
            limit=int(data.get("limit") or 0),
            hard_limit=int(hard_limit) if hard_limit is not None else None,
            granted_extra=int(data.get("granted_extra") or 0),
        )


def _command_sink_failure_reason(command: str | None, result_text: str) -> str | None:
    text = (result_text or "").strip()
    if not text:
        return None
    if command == "/ir":
        if text.startswith("IR 指令："):
            return "IR command returned usage help instead of executing"
        failure_markers = (
            "No route to host",
            "無法連線",
            "找不到",
            "名稱格式無效",
            "失敗",
            "逾時",
        )
        if any(marker in text for marker in failure_markers):
            return text
    return None


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
    literal: str | None = None       # static argument (used when no variable input, e.g. "/music playbest")

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
        if self.literal is not None:
            d["literal"] = self.literal
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
            literal=d.get("literal"),
        )


@dataclass
class Workflow:
    id: str
    goal: str
    steps: list[WorkflowStep] = field(default_factory=list)

    def validate_references(
        self,
        known_commands: frozenset[str] | None = None,
        seed_variables: Iterable[str] | None = None,
        check_types: bool = False,
    ) -> list[str]:
        """Return a list of structural errors (forward refs, unlisted commands).
        An empty list means the workflow is structurally sound.

        Pass ``known_commands`` (the keys of the live command dispatcher) to also
        flag commands that pass the denylist check but have no registered handler.
        When ``None`` (default) the registry check is skipped — used at save-time
        when no dispatcher is available.

        ``seed_variables`` are variable names that exist before the first step
        runs (results carried over from earlier work); steps may reference them
        without a producing step.

        ``check_types`` additionally flags command_sink steps whose input variable's
        statically-known type (see var_types below) isn't accepted by the sink
        (COMMAND_SINK_INPUT_TYPES) -- e.g. feeding a raw command_result into /saynow.
        Defaults to False so WorkflowRunner.run() keeps its existing runtime-only
        behavior (a real safety net independent of how the workflow was produced);
        the goal/plan drafting path opts in via check_types=True so the LLM gets a
        chance to self-repair before anything executes."""
        errors: list[str] = []
        defined: set[str] = set(seed_variables or ())
        # Statically-known output type per step kind, only tracked when
        # check_types is on. Seed variables carry no static type info at this
        # layer, so they're deliberately left out of var_types (treated as
        # unknown -- never flagged as a mismatch).
        var_types: dict[str, str] = {}
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
                elif not is_command_sink_allowed(step.command):
                    errors.append(
                        f"Step {step.id}: command '{step.command}' is not allowed "
                        f"(in denylist {sorted(COMMAND_SINK_DENYLIST)})"
                    )
                elif known_commands is not None and step.command not in known_commands:
                    errors.append(
                        f"Step {step.id}: command '{step.command}' is not registered "
                        f"(no handler found; check spelling or register the command)"
                    )
                if step.input and step.input not in defined:
                    errors.append(
                        f"Step {step.id}: input '{step.input}' is not yet produced "
                        f"by a prior step"
                    )
                elif check_types and step.input and step.command:
                    accepted = COMMAND_SINK_INPUT_TYPES.get(step.command)
                    actual = var_types.get(step.input)
                    if accepted is not None and actual is not None and actual not in accepted:
                        errors.append(
                            f"Step {step.id}: type mismatch: '{step.command}' accepts "
                            f"{sorted(accepted)} but variable '{step.input}' has type "
                            f"'{actual}' (produce it with an llm_transform step instead, "
                            f"so it's converted to plain_text/speech_text first)"
                        )
                if step.input is None and step.literal is None:
                    errors.append(
                        f"Step {step.id}: command_sink must have 'input' (variable name) "
                        f"or 'literal' (static argument)"
                    )
            elif step.kind == "llm_transform":
                for var in step.inputs:
                    if var not in defined:
                        errors.append(
                            f"Step {step.id}: input '{var}' is not yet produced "
                            f"by a prior step"
                        )
            defined.add(step.output)
            var_types[step.output] = {
                "tool_call": VARIABLE_TYPE_EVIDENCE,
                "command_sink": VARIABLE_TYPE_COMMAND_RESULT,
                "llm_transform": VARIABLE_TYPE_SPEECH_TEXT,
            }.get(step.kind, VARIABLE_TYPE_PLAIN_TEXT)
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
    budget_exhausted: WorkflowBudgetExhausted | None = None

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
        if self.budget_exhausted is not None:
            d["budget_exhausted"] = self.budget_exhausted.to_dict()
        return d


@dataclass
class WorkflowTrace:
    workflow_id: str
    goal: str
    variables: dict[str, Variable] = field(default_factory=dict)
    steps: list[StepTrace] = field(default_factory=list)
    narration: list[str] = field(default_factory=list)
    final_result: str | None = None
    # Set when the workflow fails structural validation before any step runs.
    # An empty step list alone does not mean failure; this field makes it explicit.
    validation_error: str | None = None
    budget_exhausted: WorkflowBudgetExhausted | None = None

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
            "narration": list(self.narration),
            "final_result": self.final_result,
        }
        if self.validation_error is not None:
            d["validation_error"] = self.validation_error
        if self.budget_exhausted is not None:
            d["budget_exhausted"] = self.budget_exhausted.to_dict()
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
                budget_exhausted=(
                    WorkflowBudgetExhausted.from_dict(s["budget_exhausted"])
                    if isinstance(s.get("budget_exhausted"), dict)
                    else None
                ),
            )
            for s in d.get("steps", [])
        ]
        return cls(
            workflow_id=d["workflow_id"],
            goal=d["goal"],
            variables=variables,
            steps=steps,
            narration=[str(x) for x in (d.get("narration") or [])],
            final_result=d.get("final_result"),
            validation_error=d.get("validation_error"),
            budget_exhausted=(
                WorkflowBudgetExhausted.from_dict(d["budget_exhausted"])
                if isinstance(d.get("budget_exhausted"), dict)
                else None
            ),
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

def describe_workflow_step(step: "WorkflowStep") -> str:
    if step.kind == "tool_call":
        return f"tool_call {step.tool} → {step.output}"
    if step.kind == "command_sink":
        arg = step.literal if step.literal is not None else f"${step.input}"
        return f"{step.command} {arg or ''} → {step.output}".strip()
    return f"llm_transform {step.inputs} → {step.output}"


class WorkflowRunner:
    """Executes a Workflow sequentially, binding step outputs to Variables."""

    def __init__(
        self,
        executor: ToolCallExecutor,
        command_dispatcher: CommandDispatcher | None = None,
        llm_client: LLMClient | None = None,
        step_observer: Callable[[str], None] | None = None,
        seed_variables: dict[str, str] | None = None,
        seed_variable_types: dict[str, str] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self.executor = executor
        self.command_dispatcher: CommandDispatcher = command_dispatcher or {}
        self.llm_client = llm_client
        self.step_observer = step_observer
        # Optional cooperative-cancel probe. Checked before each step so a
        # long workflow stops promptly at a safe boundary when the caller
        # cancels (issue #81); when absent, behaviour is unchanged.
        self.cancel_check = cancel_check
        self.seed_variables = dict(seed_variables or {})
        # Types (plain_text/speech_text/command_result/...) that seed variables
        # had when they were first produced. Without these every seed defaults
        # to VariableStore.bind's generic "text", which satisfies no
        # type-restricted command_sink -- carrying a speech_text result forward
        # across a replan would otherwise silently degrade it and make the
        # very next /saynow-style step fail.
        self.seed_variable_types = dict(seed_variable_types or {})

    def _observe(self, line: str) -> None:
        if self.step_observer is None:
            return
        try:
            self.step_observer(line)
        except Exception:  # noqa: BLE001
            logger.exception("workflow runner step observer failed")

    def run(self, workflow: Workflow) -> WorkflowTrace:
        """Execute all steps and return the full trace."""
        known = frozenset(self.command_dispatcher.keys()) if self.command_dispatcher else None
        errors = workflow.validate_references(
            known_commands=known, seed_variables=self.seed_variables.keys()
        )
        if errors:
            joined = "\n".join(errors)
            return WorkflowTrace(
                workflow_id=workflow.id,
                goal=workflow.goal,
                validation_error=joined,
                final_result=f"工作流定義有誤：\n{joined}",
            )

        store = VariableStore()
        for name, value in self.seed_variables.items():
            store.bind(
                name,
                str(value),
                source_step="seed",
                provenance="carried over from earlier result",
                type_=self.seed_variable_types.get(name, "text"),
            )
        trace = WorkflowTrace(workflow_id=workflow.id, goal=workflow.goal)
        failed = False
        last_output_var: str | None = None

        total = len(workflow.steps)
        cancelled = False
        for index, step in enumerate(workflow.steps, 1):
            if failed or cancelled:
                trace.steps.append(
                    StepTrace(step_id=step.id, kind=step.kind, status="skipped")
                )
                continue

            # Cooperative cancel: stop at this safe boundary rather than
            # starting another (possibly expensive) step. The already-run
            # steps' bound variables stay in the trace so a caller can still
            # use partial evidence.
            if self.cancel_check is not None and self.cancel_check():
                self._observe(f"步驟 {index}/{total}：已取消，停止後續步驟")
                trace.steps.append(
                    StepTrace(
                        step_id=step.id, kind=step.kind, status="failed",
                        error="已取消",
                    )
                )
                trace.final_result = "工作流已取消"
                cancelled = True
                continue

            self._observe(f"步驟 {index}/{total}：{describe_workflow_step(step)}")
            step_trace, produced_var = self._run_step(step, store)
            trace.steps.append(step_trace)
            if step_trace.status == "failed":
                self._observe(f"步驟 {index}/{total} 失敗：{step_trace.error or '（無詳情）'}")
            else:
                self._observe(f"步驟 {index}/{total} 完成")

            if step_trace.status == "failed":
                if step_trace.budget_exhausted is not None:
                    trace.budget_exhausted = step_trace.budget_exhausted
                    trace.final_result = (
                        f"工作流在步驟 {step.id} 暫停："
                        f"{step_trace.error or 'budget exhausted'}"
                    )
                    break
                failed = True
            elif produced_var:
                last_output_var = produced_var

        trace.variables = store.snapshot()

        if trace.budget_exhausted is not None:
            pass
        elif cancelled:
            # keep the explicit "工作流已取消" set at the cancel boundary
            pass
        elif not failed and last_output_var:
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
        exhausted = getattr(self.executor, "last_budget_exhausted", None)

        if ok:
            provenance = f"{slug}({resolved_args})"
            var = store.bind(
                step.output, result_text,
                source_step=step.id,
                provenance=provenance,
                type_=VARIABLE_TYPE_EVIDENCE,
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
                output_var=step.output,
                error=result_text,
                budget_exhausted=(
                    WorkflowBudgetExhausted(
                        kind="search",
                        used=int(
                            getattr(exhausted, "count", getattr(exhausted, "used", 0)) or 0
                        ),
                        limit=int(
                            getattr(
                                exhausted,
                                "effective_limit",
                                getattr(exhausted, "limit", 0),
                            )
                            or 0
                        ),
                        hard_limit=int(
                            getattr(
                                exhausted,
                                "hard_cap",
                                getattr(exhausted, "hard_limit", 0),
                            )
                            or 0
                        ),
                        granted_extra=int(getattr(exhausted, "granted_extra", 0) or 0),
                    )
                    if exhausted is not None
                    else None
                ),
            ),
            None,
        )

    def _run_command_sink(
        self, step: WorkflowStep, store: VariableStore
    ) -> tuple[StepTrace, str | None]:
        if not is_command_sink_allowed(step.command or ""):
            return (
                StepTrace(
                    step_id=step.id, kind=step.kind, status="failed",
                    error=f"command '{step.command}' is not allowed (denied)",
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

        # Resolve input: prefer variable reference, fall back to literal.
        if step.input:
            try:
                input_value = store.resolve(step.input)
            except KeyError as exc:
                return (
                    StepTrace(
                        step_id=step.id, kind=step.kind, status="failed",
                        error=str(exc),
                    ),
                    None,
                )
            # Type check: reject variable types the sink cannot handle.
            accepted = COMMAND_SINK_INPUT_TYPES.get(step.command)
            if accepted is not None:
                var_obj = store.get(step.input)
                if var_obj is not None and var_obj.type not in accepted:
                    return (
                        StepTrace(
                            step_id=step.id, kind=step.kind, status="failed",
                            error=(
                                f"type mismatch: '{step.command}' accepts {sorted(accepted)} "
                                f"but variable '{step.input}' has type '{var_obj.type}'"
                            ),
                        ),
                        None,
                    )
        elif step.literal is not None:
            input_value = step.literal
        else:
            input_value = ""

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

        provenance = f"{step.command}(input={step.input or repr(step.literal)})"
        result_text = str(result) if result is not None else ""
        failure_reason = _command_sink_failure_reason(step.command, result_text)
        if failure_reason is not None:
            return (
                StepTrace(
                    step_id=step.id, kind=step.kind, status="failed",
                    error=f"{step.command} failed: {failure_reason}",
                    provenance=provenance,
                ),
                None,
            )
        var = store.bind(
            step.output, result_text,
            source_step=step.id,
            provenance=provenance,
            type_=VARIABLE_TYPE_COMMAND_RESULT,
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

        # Resolve and embed each input variable with its trust class.  Context
        # and earlier model prose may clarify intent, but only user/tool/command
        # observations may ground factual claims.
        input_blocks: list[str] = []
        trusted_values: list[str] = []
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
            variable = store.get(var_name)
            variable_type = variable.type if variable is not None else "text"
            if variable_type in _TRUSTED_FACT_TYPES:
                trust = "grounding_source"
                trusted_values.append(value)
            elif variable_type == VARIABLE_TYPE_REQUIREMENT:
                trust = "requirement_only"
            else:
                trust = "untrusted_context_only"
            input_blocks.append(
                f"[{var_name}] type={variable_type} trust={trust}\n{value}"
            )

        inputs_text = "\n\n".join(input_blocks)
        prompt = (
            f"Input data:\n\n{inputs_text}\n\n"
            f"Task instructions: {step.instructions or 'Transform the input data.'}\n\n"
            "Strict rules (never break these):\n"
            "- Use ONLY information present in the input data above.\n"
            "- Only blocks marked trust=grounding_source may support factual or numeric claims.\n"
            "- Blocks marked untrusted_context_only may clarify what is being discussed, "
            "but their assistant claims are NOT evidence and must not be repeated as facts.\n"
            "- If the requested conclusion requires facts absent from grounding_source blocks, "
            "state exactly what is missing instead of filling the gap.\n"
            "- Do NOT invent, infer, or supplement facts absent from the input "
            "(e.g. temperatures, weather, locations, numbers, events).\n"
            "- Output the result only — no explanations, headings, or preamble."
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

        trusted_text = "\n".join(trusted_values)
        unsupported = unsupported_numeric_atoms(result, trusted_text)
        if unsupported:
            repair_prompt = (
                f"Grounding sources:\n{trusted_text}\n\n"
                f"Draft answer:\n{result}\n\n"
                "Rewrite the draft using only the grounding sources. The following material "
                f"numeric claims have no matching source value: {unsupported}. "
                "Remove them, or replace the affected conclusion with a clear statement of "
                "what evidence is missing. Do not add any new facts or numbers. Output only "
                "the corrected answer."
            )
            try:
                repaired = self.llm_client.generate(repair_prompt, temperature=0.2)
            except Exception:  # noqa: BLE001 - retain the structural safe fallback below
                logger.exception(
                    "task_workspace: numeric grounding repair failed step=%s", step.id
                )
                repaired = ""
            if repaired and not unsupported_numeric_atoms(repaired, trusted_text):
                result = repaired
            else:
                excerpts = "\n\n".join(value[:1600] for value in trusted_values if value.strip())
                result = (
                    "現有輸入不足以支持包含新數字的結論；以下是目前可確認的資料：\n\n"
                    f"{excerpts or '（沒有可用的事實證據）'}"
                )

        provenance = f"llm_transform(inputs={step.inputs})"
        var = store.bind(
            step.output, result,
            source_step=step.id,
            provenance=provenance,
            type_=VARIABLE_TYPE_SPEECH_TEXT,
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

    def rename(self, old_id: str, new_id: str) -> bool:
        """Rename a workflow's ID (slug). Returns False if old_id does not
        exist or new_id is already taken; True on success.

        The traces directory is moved atomically via shutil.move when present."""
        import shutil
        old_path = self._dir / f"{old_id}.json"
        new_path = self._dir / f"{new_id}.json"
        if not old_path.exists() or new_path.exists():
            return False
        wf = self.get(old_id)
        if wf is None:
            return False
        wf.id = new_id
        new_path.write_text(
            json.dumps(wf.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        old_traces = self._dir / "traces" / old_id
        if old_traces.exists():
            new_traces = self._dir / "traces" / new_id
            shutil.move(str(old_traces), str(new_traces))
        old_path.unlink()
        return True

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
