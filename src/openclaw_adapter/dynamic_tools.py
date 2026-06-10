"""Dynamic self-writing tools for the ``/new`` Telegram command.

When a request isn't covered by a fixed tool, ``DynamicToolRunner`` asks a local
Ollama model to WRITE a single-file Python tool, runs it under a lightweight
guardrail, and returns the answer. Codegen uses a model-tier cascade: a fast
code-specialized model (qwen2.5-coder:7b) writes first, escalating to the
stronger qwen3:14b (then its reasoning mode) only on repeated failure. Tools persist in
a gitignored ``generated_tools/`` folder (+ ``manifest.json``) so similar
requests can be reused instead of regenerated.

Everything is local / free — no paid frontier API.

Pipeline (see DynamicToolRunner.run_detailed):
  1. reuse-check: ask the model whether an existing manifest tool fits
  2. generate: methodology RAG + static hard rules + forced PLAN→code
  3. install ``# requires:`` packages into a dedicated venv
  4. execute under guardrail (shell=False, timeout, cwd, CLEAN_ENV strips secrets)
  5. self-repair loop (<=3); ModuleNotFoundError auto-installs without burning a try
  6. on success: extract answer, register in manifest
  7. failure distillation: if it took >=2 generations, ask the model to abstract
     the mistake into a general rule and upsert it into the codegen_knowledge RAG
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from hashlib import sha1
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_OLLAMA_MAX_RETRIES = 3       # transient-error retries in generate()
_OLLAMA_RETRY_BASE_SEC = 1.0  # first backoff; doubles each attempt (1, 2, 4 s)

ANSWER_START = "===ANSWER==="
ANSWER_END = "===END==="
_CODE_MARK = "===CODE==="
_PLAN_MARK = "===PLAN==="
_META_MARK = "===META==="
_API_STRUCT_START = "===API_STRUCT==="
_API_STRUCT_END = "===END_STRUCT==="
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL)
_REQUIRES_RE = re.compile(r"^#\s*requires:\s*(.+)$", re.MULTILINE)
_MODULE_NOT_FOUND_RE = re.compile(r"ModuleNotFoundError: No module named ['\"]([\w\.]+)['\"]")
# Safe environment variables passed through to generated scripts. Everything
# else (notably all OPENCLAW_* secrets and tokens) is stripped.
_SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR")

# Lever 2: syntax gate. Pre-check code with ast.parse before paying for a full
# subprocess run. Syntax-only repairs don't burn a generation (like the
# ModuleNotFound auto-install special case).
_MAX_SYNTAX_FIXES = 2
# Lever 1: when truncation is detected, bump the Phase-1 num_predict cap and
# regenerate rather than wasting a repair on half-written code.
_NUM_PREDICT_BUMP = 3000
# Truncation signatures: the model's output was cut mid-statement, so ast.parse
# reports an unexpected end-of-file / unterminated construct.
_TRUNCATION_MARKERS = (
    "unexpected eof",
    "unterminated",
    "was never closed",
    "expected an indented block",
    "eof in multi-line",
)


def _syntax_error(code: str) -> str:
    """Return a one-line SyntaxError description, or "" if the code parses."""
    try:
        ast.parse(code)
    except SyntaxError as exc:
        line = exc.lineno if exc.lineno is not None else "?"
        return f"{exc.msg} (line {line})"
    return ""


def _is_truncation_error(msg: str) -> bool:
    low = msg.lower()
    return any(marker in low for marker in _TRUNCATION_MARKERS)


def _is_thinking_model(model: str) -> bool:
    """qwen3 family supports /think /no_think prompt directives; others don't."""
    return (model or "").strip().lower().startswith("qwen3")


# Stdlib modules the generated tools routinely use. A missing import of one of
# these is a runtime NameError (not ModuleNotFoundError, since they're built in),
# which ast.parse can't catch and the repair loop wastes generations on. We add
# the import deterministically instead.
_AUTO_IMPORT_STDLIB = frozenset({
    "os", "sys", "json", "re", "math", "datetime", "time", "random",
    "statistics", "decimal", "collections", "itertools", "functools",
    "csv", "html", "base64", "hashlib", "textwrap", "urllib",
})


def _ensure_stdlib_imports(code: str) -> str:
    """Prepend `import <mod>` for any safelisted stdlib module that is used as a
    bare `mod.<attr>` but never imported/bound. Deterministic, no model call."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code  # caller's syntax gate handles this
    bound: set[str] = set()
    used_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                bound.add(node.id)
            elif isinstance(node.ctx, ast.Load):
                used_roots.add(node.id)
    missing = sorted(
        m for m in used_roots & _AUTO_IMPORT_STDLIB if m not in bound
    )
    if not missing:
        return code
    logger.info("dynamic_tools: auto-adding missing stdlib imports=%s", missing)
    header = "".join(f"import {m}\n" for m in missing)
    return header + code


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class DynamicToolResult:
    ok: bool
    answer: str = ""
    slug: str = ""
    reused: bool = False
    generations: int = 0
    error: str = ""
    raw_stdout: str = ""


def probe_ollama(endpoint: str, *, timeout: float = 3.0) -> bool:
    """Return True when Ollama's /api/tags endpoint responds within *timeout* s."""
    base = endpoint.rstrip("/")
    for suffix in ("/api/generate", "/api"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    try:
        with urlopen(Request(f"{base}/api/tags", method="GET"), timeout=timeout):
            return True
    except Exception:
        return False


class OllamaTextClient:
    """Minimal stdlib POST to Ollama /api/generate (non-streaming)."""

    def __init__(self, *, endpoint: str, model: str, timeout_seconds: int,
                 num_ctx: int | None = None, num_predict: int | None = None) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout_seconds = max(1, timeout_seconds)
        self.num_ctx = num_ctx
        self.num_predict = num_predict

    def _url(self) -> str:
        path = self.endpoint
        if path.endswith("/api/generate"):
            return path
        if path.endswith("/api"):
            return f"{path}/generate"
        return f"{path}/api/generate"

    def generate(self, prompt: str, *, temperature: float = 0.0, think: bool = False) -> str:
        # qwen3 respects /no_think / /think directives in the prompt prefix.
        # This is more reliable than the "think" API option across Ollama versions.
        # Non-thinking models (e.g. qwen2.5-coder) have no such mode, so the
        # directive is just spurious prompt text there — only prepend for qwen3.
        if think or not _is_thinking_model(self.model):
            full_prompt = prompt
        else:
            full_prompt = f"/no_think\n{prompt}"
        options: dict = {"temperature": temperature}
        if self.num_ctx is not None:
            options["num_ctx"] = self.num_ctx
        if self.num_predict is not None:
            options["num_predict"] = self.num_predict
        payload = {
            "model": self.model,
            "prompt": full_prompt,
            "stream": False,
            "options": options,
        }
        request = Request(
            self._url(),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        last_exc: RuntimeError | None = None
        body = ""
        for attempt in range(1, _OLLAMA_MAX_RETRIES + 1):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read().decode("utf-8", errors="replace")
                last_exc = None
                break
            except HTTPError as exc:
                if exc.code < 500:
                    raise RuntimeError(f"Ollama HTTP {exc.code}") from exc
                last_exc = RuntimeError(f"Ollama HTTP {exc.code}")
            except URLError as exc:
                last_exc = RuntimeError(f"Ollama request failed: {exc.reason}")
            if attempt < _OLLAMA_MAX_RETRIES:
                delay = _OLLAMA_RETRY_BASE_SEC * (2.0 ** (attempt - 1))
                logger.warning(
                    "Ollama transient error attempt %d/%d; retrying in %.0fs: %s",
                    attempt, _OLLAMA_MAX_RETRIES, delay, last_exc,
                )
                time.sleep(delay)
        if last_exc is not None:
            raise RuntimeError(
                f"Ollama 不在線或無回應（已重試 {_OLLAMA_MAX_RETRIES} 次）"
            ) from last_exc
        parsed = json.loads(body)
        text = parsed.get("response", "")
        if not isinstance(text, str):
            raise RuntimeError(f"Ollama response type was {type(text).__name__}")
        return _THINK_RE.sub("", text).strip()


class DynamicToolRunner:
    def __init__(
        self,
        *,
        client: OllamaTextClient,
        tools_dir: Path,
        knowledge_db: "object | None" = None,
        exec_timeout_seconds: int = 90,
        max_repairs: int = 3,
        base_python: str | None = None,
        distill_enabled: bool = False,
        fast_model: str | None = None,
        strong_model: str | None = None,
    ) -> None:
        self.client = client
        # Cascade: fast_model handles explore + tier-1 codegen; strong_model is
        # only loaded on escalation. Both default to the client's own model, so
        # a single-model config degenerates to the old single-tier behavior.
        self.fast_model = fast_model or client.model
        self.strong_model = strong_model or client.model
        self.client.model = self.fast_model
        self.tools_dir = Path(tools_dir)
        self.knowledge_db = knowledge_db
        self.exec_timeout_seconds = exec_timeout_seconds
        self.max_repairs = max_repairs
        self.base_python = base_python or sys.executable
        # Auto-distillation writes the LOCAL model's own abstracted rules into the
        # RAG. Off by default — the user wants Claude-taught/seed rules only.
        self.distill_enabled = distill_enabled
        # Last codegen META block (tool_type + param_schema), parsed per generation.
        self._last_meta: dict | None = None
        self.tools_dir.mkdir(parents=True, exist_ok=True)

    # ── public ──────────────────────────────────────────────────────────────

    def run(self, request: str) -> str:
        """Telegram-facing entry: returns a human-readable string."""
        req = (request or "").strip()
        if not req:
            return "用法：/new <你的需求>，例如 /new 幫我查0050今年以來到5月的年化報酬"
        try:
            result = self.run_detailed(req)
        except Exception as exc:  # defensive — never crash the bot loop
            logger.exception("dynamic_tools: run failed")
            return f"動態工具執行失敗：{exc}"
        if result.ok:
            prefix = "♻️ 重用既有工具\n" if result.reused else "🛠 新生成工具\n"
            return prefix + result.answer
        return f"⚠️ 無法完成（生成 {result.generations} 次仍失敗）\n{result.error}"

    def run_detailed(self, request: str) -> DynamicToolResult:
        req = request.strip()
        # Separate WHAT data to fetch from HOW to display it. The layout/template
        # portion otherwise pollutes reuse-matching (the classifier sees a wall of
        # template text and picks NEW, regenerating a tool that already exists) and
        # gets baked into the tool. Matching/slug/codegen use the clean core; the
        # format spec is applied once at the end, on either path.
        core, format_spec = self._split_request_intent(req)

        result: DynamicToolResult | None = None
        match = self._pick_reusable(core)
        if match is not None:
            reused = self._reuse(match, core)
            if reused is not None and reused.ok:
                result = reused
            else:
                logger.info("dynamic_tools: reuse failed, regenerating slug=%s", match.get("slug"))
        if result is None:
            result = self._generate_with_repair(core)

        if result.ok and format_spec:
            result.answer = self._apply_presentation(format_spec, result.answer)
        return result

    def _split_request_intent(self, request: str) -> tuple[str, str]:
        """Split a request into (core_data_request, format_spec). The format spec
        is the user's layout/template text ("" when none). Keeping layout out of
        the core lets reuse-matching, slug, and codegen see only the data need."""
        prompt = (
            "把下面的需求拆成兩部分，只輸出一個 JSON 物件：\n"
            '{"core": "<要查什麼資料的精簡描述，去掉任何排版/格式/範例指示>", '
            '"format": "<使用者指定的輸出格式或範例原文；沒有就空字串>"}\n'
            "規則：core 只保留『要查的資料種類與標的』（例如城市、股票、日期），"
            "不可包含任何『格式如下』『版型』『emoji 範例』等排版指示；"
            "format 原樣保留使用者貼的版型/範例文字（含 emoji 與欄位），沒有就給空字串。\n"
            "⚠️ 最重要：『格式範例』裡出現的地名／數字／日期只是版型示意，"
            "不是使用者真正要查的標的；core 必須完整保留使用者真正要查的標的"
            "（即使範例裡寫的是別的地名也一樣，不可被範例帶偏而把真正標的刪掉）。\n"
            "範例：需求「幫我查 大阪 的天氣 格式如下\\n📍 札幌市\\n氣溫：17°C」"
            "→ 正確拆法 core=\"大阪的天氣\"（保留大阪，不是範例裡的札幌），"
            'format="📍 札幌市\\n氣溫：17°C"。\n'
            "只輸出 JSON，不要解說。\n\n"
            f"需求：{request}\n"
        )
        try:
            raw = self.client.generate(prompt, temperature=0.0)
            data = _load_json_object(raw)
        except Exception:
            logger.exception("dynamic_tools: intent split failed; using request as-is")
            return request, ""
        if not data:
            return request, ""
        core = str(data.get("core") or "").strip() or request
        fmt = str(data.get("format") or "").strip()
        return core, fmt

    def _reuse(self, entry: dict, request: str) -> DynamicToolResult | None:
        """Run an existing tool for a new request. For parameterized tools
        (entry has param_schema) we extract fresh params from the request and
        write params.json before running — so the same tool serves any same-type
        question without regeneration. Legacy tools (no schema) run as-is."""
        slug = entry.get("slug", "")
        tool_path = self.tools_dir / slug / "tool.py"
        if not tool_path.exists():
            return None
        schema = entry.get("param_schema")
        if schema:
            params = self._extract_params(schema, request)
            if not params:
                logger.info("dynamic_tools: param extraction failed for slug=%s, regenerating", slug)
                return None
            (self.tools_dir / slug / "params.json").write_text(
                json.dumps(params, ensure_ascii=False), encoding="utf-8"
            )
            logger.info("dynamic_tools: reusing slug=%s with injected params=%s", slug, params)
        else:
            logger.info("dynamic_tools: reusing legacy slug=%s as-is for request=%s", slug, request[:80])
        code = tool_path.read_text(encoding="utf-8")
        result = self._install_and_execute(slug, tool_path, code)
        if result.ok:
            result.reused = True
            return result
        return None

    def _extract_params(self, schema: list, request: str) -> dict | None:
        """Ask the model to pull concrete parameter values out of the request,
        constrained to the tool's declared param_schema. Returns {} -> None."""
        names = [s.get("name") for s in schema if isinstance(s, dict) and s.get("name")]
        if not names:
            return None
        prompt = (
            "從下列需求中抽取參數值，只輸出一個 JSON 物件（key 為參數名，value 為數值或字串）。\n"
            "參數定義（name/type/說明）：\n"
            + json.dumps(schema, ensure_ascii=False)
            + f"\n\n需求：{request}\n\n"
            "規則：只輸出 JSON，不要任何解說；數值用純數字（不要逗號、不要單位）；"
            "找不到的參數就省略該 key。"
        )
        try:
            raw = self.client.generate(prompt, temperature=0.0)
        except Exception:
            logger.exception("dynamic_tools: param extraction call failed")
            return None
        data = _load_json_object(raw)
        if not data:
            return None
        clean = {k: v for k, v in data.items() if k in names}
        return clean or None

    def _apply_presentation(self, format_spec: str, data_text: str) -> str:
        """Reshape a tool's raw output to match a requested layout. The data is
        already correct — this pass only *re-arranges* it, never invents values —
        so any path (reuse or fresh generation) can satisfy a new layout without
        baking the format into the tool. Failure falls back to raw data."""
        prompt = (
            "下面是一支工具產生的『正確資料』。請依指定的『輸出格式/範例』把這些資料重新排版。\n"
            "重新排版的嚴格規則：\n"
            "- 只能使用『資料』裡已有的數值/文字；絕對不可捏造、推算、或補資料沒有的欄位。\n"
            "- 範例裡的示意值（日期、地名、數字、天氣描述）只是版型示範，必須換成『資料』裡的"
            "真實值，絕不可照抄範例的值。\n"
            "- 格式要求某欄位但資料沒有，就略過該欄位，不要編造（寧缺勿假）。\n"
            "- 只輸出最終排版結果，不要任何解說、前言或程式碼框。\n\n"
            f"輸出格式/範例：\n{format_spec}\n\n資料：\n{data_text}\n"
        )
        try:
            out = self.client.generate(prompt, temperature=0.0).strip()
        except Exception:
            logger.exception("dynamic_tools: presentation pass failed; using raw data")
            return data_text
        return out or data_text

    # ── generation + self-repair ────────────────────────────────────────────

    def _generate_with_repair(self, request: str) -> DynamicToolResult:
        knowledge_rows = self._retrieve_knowledge(request)

        # Phase 0: API exploration — run a tiny discovery script to capture actual
        # field names from the real API response before writing the real tool.
        api_structure = self._explore_api(request, knowledge_rows)

        slug = self._make_slug(request)
        tool_dir = self.tools_dir / slug
        tool_dir.mkdir(parents=True, exist_ok=True)
        tool_path = tool_dir / "tool.py"

        generations = 0
        last_error = ""
        last_stdout = ""

        # The escalation path mutates client.model / num_predict / timeout; the
        # syntax gate may also bump num_predict. Save and restore so a complex
        # request doesn't leak a relaxed cap or model name into the next one.
        saved_num_predict = self.client.num_predict
        saved_timeout = self.client.timeout_seconds
        try:
            # Model-tier cascade. Tier A: fast code-specialized model (think=False).
            # Tier B: strong model, still fast. Tier C: strong model, reasoning.
            # Only a real failure climbs a tier, so the common case never loads
            # the heavy model. num_ctx stays constant across tiers (changing it
            # forces an Ollama reload; num_predict does not).
            for phase, model, think, phase_max in (
                (1, self.fast_model,   False, self.max_repairs),
                (2, self.strong_model, False, self.max_repairs),
                (3, self.strong_model, True,  1),
            ):
                self.client.model = model
                if phase >= 2:
                    logger.info(
                        "dynamic_tools: escalating to tier %s model=%s think=%s for request=%s",
                        phase, model, think, request[:80],
                    )
                if phase == 3:
                    # Think mode generates longer output; remove the num_predict cap and
                    # extend the HTTP timeout to 1200s.
                    self.client.num_predict = None
                    self.client.timeout_seconds = max(self.client.timeout_seconds, 1200)
                code = self._generate_code(request, knowledge_rows, think=think,
                                           api_structure=api_structure)
                code = self._pass_syntax_gate(request, code, knowledge_rows,
                                              think=think, api_structure=api_structure, phase=phase)
                phase_gen = 1
                while True:
                    generations += 1
                    tool_path.write_text(code, encoding="utf-8")
                    exec_result = self._install_and_execute(slug, tool_path, code)
                    last_stdout = exec_result.raw_stdout
                    if exec_result.ok:
                        exec_result.slug = slug
                        exec_result.generations = generations
                        meta = self._last_meta or {}
                        self._register_manifest(
                            slug, request, code, self._parse_requires(code),
                            tool_type=meta.get("tool_type"),
                            param_schema=meta.get("param_schema"),
                        )
                        if generations >= 2 and self.distill_enabled:
                            self._distill_failure(request, code, last_error)
                        self._mark_knowledge_applied(knowledge_rows)
                        return exec_result
                    last_error = exec_result.error
                    if phase_gen >= phase_max:
                        break
                    phase_gen += 1
                    code = self._repair_code(request, code, last_error, knowledge_rows,
                                             think=think, api_structure=api_structure)
                    code = self._pass_syntax_gate(request, code, knowledge_rows,
                                                  think=think, api_structure=api_structure, phase=phase)

            return DynamicToolResult(
                ok=False, slug=slug, generations=generations,
                error=_tail(last_error, 600), raw_stdout=last_stdout,
            )
        finally:
            self.client.num_predict = saved_num_predict
            self.client.timeout_seconds = saved_timeout
            self.client.model = self.fast_model

    def _pass_syntax_gate(
        self, request: str, code: str, knowledge_rows, *,
        think: bool, api_structure: str | None, phase: int,
    ) -> str:
        """Lever 2: ast.parse the code before paying for a subprocess run.

        Syntax-only repairs don't count as a generation. Lever 1: if the failure
        looks like truncation (output cut mid-statement) and we're in the capped
        Phase 1, bump num_predict and regenerate from scratch instead of trying
        to "repair" half-written code.
        """
        for _ in range(_MAX_SYNTAX_FIXES):
            err = _syntax_error(code)
            if not err:
                # Code parses; deterministically repair missing stdlib imports
                # (a runtime NameError ast.parse can't see) before execution.
                return _ensure_stdlib_imports(code)
            logger.info("dynamic_tools: syntax gate caught: %s", err)
            if (phase == 1 and _is_truncation_error(err)
                    and self.client.num_predict is not None
                    and self.client.num_predict < _NUM_PREDICT_BUMP):
                logger.info(
                    "dynamic_tools: truncation detected, bumping num_predict %s->%s and regenerating",
                    self.client.num_predict, _NUM_PREDICT_BUMP,
                )
                self.client.num_predict = _NUM_PREDICT_BUMP
                code = self._generate_code(request, knowledge_rows, think=think,
                                           api_structure=api_structure)
                continue
            code = self._repair_code(request, code, f"SyntaxError: {err}", knowledge_rows,
                                     think=think, api_structure=api_structure)
        return code

    def _install_and_execute(self, slug: str, tool_path: Path, code: str) -> DynamicToolResult:
        """Install declared requires, execute, with one extra retry purely for an
        auto-installable ModuleNotFoundError (doesn't count as a generation)."""
        requires = self._parse_requires(code)
        if requires:
            self._pip_install(requires)
        for _ in range(2):  # initial + one auto-install retry
            proc = self._execute(tool_path)
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            if proc.returncode == 0:
                answer = _extract_answer(stdout)
                if answer:
                    return DynamicToolResult(ok=True, answer=answer, slug=slug, raw_stdout=stdout)
                return DynamicToolResult(
                    ok=False, slug=slug, raw_stdout=stdout,
                    error="腳本成功執行但找不到 ===ANSWER=== 區塊。\nstdout:\n" + _tail(stdout, 400),
                )
            missing = _MODULE_NOT_FOUND_RE.search(stderr)
            if missing:
                pkg = missing.group(1).split(".")[0]
                logger.info("dynamic_tools: auto-installing missing module=%s", pkg)
                self._pip_install((pkg,))
                continue
            return DynamicToolResult(
                ok=False, slug=slug, raw_stdout=stdout,
                error=_tail(stderr, 600) or f"非零退出碼 {proc.returncode}",
            )
        # exhausted auto-install retry
        return DynamicToolResult(ok=False, slug=slug, error="缺少套件且自動安裝後仍失敗。")

    def _execute(self, tool_path: Path) -> subprocess.CompletedProcess:
        venv_python = self._ensure_venv()
        tool_dir = tool_path.parent
        env = self._clean_env(tool_dir)
        try:
            return subprocess.run(
                [str(venv_python), str(tool_path)],
                shell=False,
                cwd=str(tool_dir),
                timeout=self.exec_timeout_seconds,
                capture_output=True,
                text=True,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(
                args=exc.cmd, returncode=124,
                stdout=exc.stdout or "", stderr=f"執行逾時（>{self.exec_timeout_seconds}s）",
            )

    def _clean_env(self, tool_dir: Path) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in _SAFE_ENV_KEYS:
            if key in os.environ:
                env[key] = os.environ[key]
        # Sandbox cache/config writes into the tool's own dir; deliberately do
        # NOT expose the real HOME (avoids leaking creds/cache).
        env["HOME"] = str(tool_dir)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        return env

    # ── venv / pip ──────────────────────────────────────────────────────────

    def _venv_dir(self) -> Path:
        return self.tools_dir / ".venv"

    def _venv_python(self) -> Path:
        return self._venv_dir() / "bin" / "python"

    def _ensure_venv(self) -> Path:
        python = self._venv_python()
        if python.exists():
            return python
        logger.info("dynamic_tools: creating dedicated venv at %s", self._venv_dir())
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [self.base_python, "-m", "venv", str(self._venv_dir())],
            check=True, capture_output=True, text=True,
        )
        if not python.exists():
            raise RuntimeError(f"venv python still missing after creation: {python}")
        return python

    def _pip_install(self, packages: tuple[str, ...]) -> None:
        safe = tuple(p for p in packages if p and _is_safe_pkg(p))
        blocked = [p for p in safe if not _is_approved_pkg(p)]
        if blocked:
            msg = (
                f"⛔ /new: 以下套件不在核准清單，已拒絕安裝：{', '.join(blocked)}\n"
                "如需使用，請請求管理員將套件加入 _APPROVED_PACKAGES。"
            )
            logger.warning("dynamic_tools: blocked unapproved packages: %s", blocked)
            raise RuntimeError(msg)
        pkgs = tuple(p for p in safe if _is_approved_pkg(p))
        if not pkgs:
            return
        python = self._ensure_venv()
        logger.info("dynamic_tools: pip install %s", " ".join(pkgs))
        proc = subprocess.run(
            [str(python), "-m", "pip", "install", "--quiet", *pkgs],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            logger.warning("dynamic_tools: pip install failed: %s", _tail(proc.stderr, 300))

    def _parse_requires(self, code: str) -> tuple[str, ...]:
        out: list[str] = []
        for match in _REQUIRES_RE.finditer(code):
            for token in re.split(r"[\s,]+", match.group(1).strip()):
                token = token.strip()
                low = token.lower()
                if not token or not _is_safe_pkg(token):
                    continue
                if low in _REQUIRES_STOPWORDS or low in _STDLIB_MODULES:
                    continue  # "none"/"無"/stdlib mentions aren't pip packages
                out.append(token)
        # de-dup, preserve order
        seen: set[str] = set()
        return tuple(p for p in out if not (p in seen or seen.add(p)))

    # ── model prompts ───────────────────────────────────────────────────────

    def _explore_api(self, request: str, knowledge_rows: list) -> str | None:
        """Phase 0: generate + run a tiny discovery script to capture actual API
        field names. Returns the captured structure text, or None if exploration
        fails / is not needed (pure-computation requests)."""
        explorer_prompt = (
            "你是 Python 工程師。請為以下需求寫一個「API 探索腳本」（不是最終工具）。\n"
            "目的：呼叫相關 API 一次，把回傳的 JSON 欄位結構印出，供後續正式腳本使用正確欄位名。\n\n"
            f"需求：{request}\n\n"
            "規則：\n"
            f'1. 若需求需要外部 API（天氣、股票、匯率等）：呼叫 API，然後：\n'
            f'   print("{_API_STRUCT_START}")\n'
            '   print(json.dumps(response, indent=2, ensure_ascii=False)[:1200])\n'
            f'   print("{_API_STRUCT_END}")\n'
            '2. 若需求是純計算（不需外部 API，資料已在 request 中）：只 print("NO_EXTERNAL_API")\n'
            "3. 失敗時 sys.exit(1)；不要 ===ANSWER=== 標記；只用 stdlib+urllib（不要 # requires）。\n"
            "推薦端點（依需求選擇）：\n"
            "  天氣：wttr.in/{city}?format=j1 —— 直接接受城市名（city 須 urllib.parse.quote 編碼）\n"
            "        勿使用 Nominatim / open-meteo（Nominatim 403、open-meteo 需座標）\n"
            "  股票：Yahoo Finance chart API（https://query1.finance.yahoo.com/v8/finance/chart/...）\n"
            "直接輸出 Python 程式碼，不加說明："
        )
        saved_np = self.client.num_predict
        saved_to = self.client.timeout_seconds
        try:
            self.client.num_predict = 500
            self.client.timeout_seconds = max(120, saved_to // 4)
            raw = self.client.generate(explorer_prompt, temperature=0.0, think=False)
            explorer_code = _extract_code(raw)
        except Exception as exc:
            logger.info("dynamic_tools: explorer generation failed: %s", exc)
            return None
        finally:
            self.client.num_predict = saved_np
            self.client.timeout_seconds = saved_to

        if not explorer_code.strip():
            return None

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / "explorer.py"
            tmp_path.write_text(explorer_code, encoding="utf-8")
            try:
                venv_python = self._ensure_venv()
                env = self._clean_env(Path(tmpdir))
                proc = subprocess.run(
                    [str(venv_python), str(tmp_path)],
                    shell=False, cwd=tmpdir, timeout=35,
                    capture_output=True, text=True, env=env,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.info("dynamic_tools: explorer execution failed: %s", exc)
                return None

            stdout = proc.stdout or ""
            if "NO_EXTERNAL_API" in stdout:
                logger.info("dynamic_tools: explorer says NO_EXTERNAL_API, skipping injection")
                return None
            if proc.returncode != 0 or _API_STRUCT_START not in stdout:
                logger.info("dynamic_tools: explorer produced no struct (rc=%d, stderr=%s)",
                            proc.returncode, _tail(proc.stderr or "", 200))
                return None

            struct = _extract_api_struct(stdout)
            if struct:
                logger.info("dynamic_tools: captured API structure (%d chars)", len(struct))
                return struct
        return None

    def _generate_code(self, request: str, knowledge_rows: list, *, think: bool = False,
                       api_structure: str | None = None) -> str:
        prompt = self._build_codegen_prompt(request, knowledge_rows, api_structure=api_structure)
        response = self.client.generate(prompt, temperature=0.0, think=think)
        self._last_meta = _extract_meta(response)
        return _extract_code(response)

    def _repair_code(self, request: str, code: str, error: str, knowledge_rows: list, *,
                     think: bool = False, api_structure: str | None = None) -> str:
        today = date.today().isoformat()
        api_block = ""
        if api_structure:
            api_block = (
                "\n<API實際回傳結構（請務必使用這些真實欄位名）>\n"
                + api_structure
                + "\n</API實際回傳結構>\n"
            )
        prompt = (
            "你先前寫的 Python 腳本執行失敗，請修正後重寫整支腳本。\n\n"
            f"今天日期：{today}\n"
            f"原始需求：{request}\n"
            + api_block
            + f"\n前一版原始碼：\n{code}\n\n"
            f"執行錯誤/stderr：\n{_tail(error, 800)}\n\n"
            + self._rules_block(knowledge_rows)
            + "\n請重新輸出三段：" + _PLAN_MARK + "、" + _META_MARK + "（tool_type+param_schema 的合法 JSON）、"
            + _CODE_MARK + "。CODE 區塊是完整可獨立執行的 python，不要加 markdown 圍欄。"
        )
        response = self.client.generate(prompt, temperature=0.0, think=think)
        new_meta = _extract_meta(response)
        if new_meta:
            self._last_meta = new_meta
        return _extract_code(response)

    def _build_codegen_prompt(self, request: str, knowledge_rows: list,
                              api_structure: str | None = None) -> str:
        today = date.today().isoformat()
        api_block = ""
        if api_structure:
            api_block = (
                "\n<API實際回傳結構（以下是真實 API 回傳的欄位名稱，請務必使用，不要自創或猜測）>\n"
                + api_structure
                + "\n</API實際回傳結構>\n"
            )
        return (
            "你是資深 Python 工程師。請為以下需求寫一支「單檔、可獨立執行」的 Python 3 腳本。\n\n"
            f"今天日期：{today}\n"
            f"需求：{request}\n"
            + api_block
            + "\n" + self._rules_block(knowledge_rows)
            + "\n請依序輸出三段，格式嚴格如下：\n"
            + _PLAN_MARK + "\n<簡短計畫：資料源、edge case、輸出格式、有哪些可被替換的輸入參數>\n"
            + _META_MARK + "\n"
            '{"tool_type": "<這支工具的抽象功能類型，例如：房貸等額本息月供、年金終值、城市天氣查詢>", '
            '"param_schema": [{"name": "<與程式裡 DEFAULTS 的 key 完全一致>", '
            '"type": "number|string", "desc": "<說明>"}]}\n'
            + _CODE_MARK + "\n<完整 python 程式碼>\n\n"
            "只輸出上述三段；META 必須是合法 JSON；CODE 區塊不要加 markdown 圍欄、不要其他解說。"
        )

    def _rules_block(self, knowledge_rows: list) -> str:
        from .knowledge_db import format_codegen_knowledge_block

        methodology = format_codegen_knowledge_block(knowledge_rows) if knowledge_rows else "(無)"
        return (
            "可用環境：Python 3 標準函式庫；需要第三方套件時，在檔案最上方用註解列出，"
            "例如：# requires: yfinance（會自動 pip install 到專屬 venv）。\n"
            "優先使用標準函式庫 urllib + 公開 JSON API 以減少相依與加快執行。\n\n"
            "天氣查詢 → 使用 wttr.in（直接接受城市名，無需座標，免 API key）：\n"
            "  import urllib.parse\n"
            "  city_enc = urllib.parse.quote(city_name, safe='')\n"
            "  url = f'https://wttr.in/{city_enc}?format=j1'\n"
            "  # 帶 User-Agent header 避免 403\n"
            "  req = urllib.request.Request(url, headers={'User-Agent': 'WeatherBot/1.0'})\n"
            "  回傳結構：current_condition[0].temp_C（現在氣溫），\n"
            "    weather[0].maxtempC / weather[0].mintempC（今日最高/最低），\n"
            "    weather[0].hourly 各時段 chanceofrain → max() 取最高降雨機率，\n"
            "    current_condition[0].weatherDesc[0].value（天氣描述文字）\n"
            "  ⚠️ 勿使用 Nominatim（403 Forbidden）＋open-meteo 的二段式流程。\n\n"
            "Yahoo Finance chart API（台股/美股日線都可用，務必帶 User-Agent header）：\n"
            "  GET https://query1.finance.yahoo.com/v8/finance/chart/<symbol>"
            "?period1=<unix_ts>&period2=<unix_ts>&interval=1d&events=div\n"
            "  台股代號加 .TW（如 0050.TW），美股直接用代號（如 TSLA）。\n"
            "  回傳 JSON 結構（請用這些確切路徑取值，不要自創 key 如 'data'）：\n"
            "    r = json['chart']['result'][0]\n"
            "    時間戳: r['timestamp']  # list[int]，秒\n"
            "    收盤價(價格報酬用): r['indicators']['quote'][0]['close']  # list，可能含 None\n"
            "    還原收盤價(含息總報酬用): r['indicators']['adjclose'][0]['adjclose']  # list\n"
            "    配息: r['events']['dividends']  # dict，值為 {amount, date}\n"
            "  close/adjclose 陣列**一定含 None**（停牌日），取起點/終點前必須過濾：\n"
            "    prices = [p for p in raw_prices if p is not None]\n"
            "    start_price, end_price = prices[0], prices[-1]\n"
            "  千萬不要直接用 raw_prices[0] 或 raw_prices[-1]，那可能是 None。\n\n"
            "<代碼開發方法論>\n" + methodology + "\n</代碼開發方法論>\n\n"
            "參數化（重要——讓工具能被重用）：\n"
            "  把需求中『可替換的輸入值』（金額、利率、年期、城市、股票代碼、日期、清單資料等）"
            "做成參數，不要散落寫死在計算式裡。在程式開頭這樣寫：\n"
            "    import json, os\n"
            "    DEFAULTS = {  # 本次需求的實際值\n"
            "        # 例：'total_price': 10000000, 'down_payment': 4000000, 'annual_rate': 0.03, 'years': 20\n"
            "    }\n"
            "    params = dict(DEFAULTS)\n"
            "    if os.path.exists('params.json'):\n"
            "        params.update(json.load(open('params.json', encoding='utf-8')))\n"
            "  之後所有計算都用 params[...]；不要再用字面值。\n"
            "  META 的 param_schema 每個 name 必須與 DEFAULTS 的 key 完全一致。\n"
            "  連『計算依據／計算方式』那句說明也必須用 f-string 帶入 params[...] 的實際值，"
            "嚴禁把數字寫死在說明字串裡（例如不可寫『年利率3.5%、25年期』這種字面字串——"
            "工具被重用換參數後那句會變成謊話；要寫成 "
            "f\"年利率{params['annual_rate']*100}%、{params['years']}年期\"）。\n"
            "  ⚠️ 最容易犯的錯：把『標的名稱』寫死在輸出文字裡。答案句子裡提到的『查的是什麼』"
            "（城市名、股票名/代碼、公司名、日期）也是參數，必須用 params[...] 帶入，"
            "絕不可寫死字面值——否則工具換參數重用後，數據對了但標的講錯，變成答非所問。\n"
            "    ❌ 反例：DEFAULTS={'city':'Paris'}，卻 print(f\"巴黎現在氣溫{temp}°C\")"
            "（換成倫敦重用時會抓倫敦的溫度、卻還是印『巴黎』）。\n"
            "    ✅ 正解：print(f\"{params['city']}現在氣溫{temp}°C\")——標的跟著參數走。\n\n"
            "  ⚠️ 輸出乾淨的『資料值』，不要輸出原始結構或連結：\n"
            "    - API 欄位常是巢狀物件或清單（例如 {'value':'晴天'} 或 [{'value':'Aichi'}]），"
            "要取出裡面的純量值再輸出（data[...][0]['value']），絕不可直接 print 出 dict/list。\n"
            "    - 要『天氣描述/狀態』就取描述文字欄位，不要輸出圖片連結或 URL 當描述"
            "（weatherIconUrl 之類是圖檔網址，不是給人看的描述）。\n"
            "    - 數字做比較或運算前先轉型：很多 API 把數字當字串回傳（\"0\"），"
            "直接 \"0\"==0 會是 False，要先 int()/float() 再比較，否則判斷會相反。\n"
            "  ⚠️ 不要把使用者的『輸出版型/emoji 範例』寫死進工具：工具只負責輸出乾淨的資料"
            "（標的、數值、描述文字），排版由外層格式層處理；把一次性版型寫死會讓工具僵化又易錯。\n\n"
            "硬性規則：\n"
            "1. 不可讀取任何祕密環境變數（OPENCLAW_*、API token 等）；"
            "讀取同目錄的 params.json 是允許且必要的（那不是祕密）。\n"
            "2. 不可刪除檔案、不可開 shell/subprocess。\n"
            f"3. 成功時最終答案必須印在標記之間：先 print(\"{ANSWER_START}\")，"
            f"接著 print 人類可讀答案（含數值與一句『怎麼算的』），最後 print(\"{ANSWER_END}\")。\n"
            "4. 數值任務務必明講資料源、期間、用的公式（年化分簡單/複利、報酬分價格/含息）；"
            "報酬與年化請以百分比輸出（記得乘以 100，例如 0.6 要寫成 60%）。\n"
            f"5. 失敗時（抓不到資料、結構不符、計算不出來）**不要**把錯誤訊息印進 {ANSWER_START} 區塊，"
            "而是要 raise 例外或 sys.exit(1) 讓程式以非零碼結束 —— 這樣外層才能觸發自動修復重寫。\n"
        )

    # ── reuse / manifest ────────────────────────────────────────────────────

    def _manifest_path(self) -> Path:
        return self.tools_dir / "manifest.json"

    def _load_manifest(self) -> list[dict]:
        path = self._manifest_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_manifest(self, entries: list[dict]) -> None:
        self._manifest_path().write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _register_manifest(self, slug: str, request: str, code: str, requires: tuple[str, ...],
                           *, tool_type: str | None = None, param_schema: list | None = None) -> None:
        entries = [e for e in self._load_manifest() if e.get("slug") != slug]
        entry = {
            "id": sha1(slug.encode("utf-8")).hexdigest()[:12],
            "slug": slug,
            "request": request,
            "description": _first_line(request, 120),
            "requires": list(requires),
            "created_at": _utc_now_iso(),
            "path": str((self.tools_dir / slug / "tool.py").relative_to(self.tools_dir)),
        }
        if not (isinstance(param_schema, list) and param_schema):
            param_schema = _defaults_schema_from_code(code)  # META unreliable → read code
        if isinstance(param_schema, list) and param_schema:
            entry["param_schema"] = param_schema
            # A parameterized tool must carry a tool_type so it can be matched for
            # reuse; if META omitted one, derive a stable label from its key shape.
            if not tool_type:
                tool_type = "params(" + ",".join(p["name"] for p in param_schema) + ")"
        if tool_type:
            entry["tool_type"] = str(tool_type)
        entries.append(entry)
        self._save_manifest(entries)

    def _pick_reusable(self, request: str) -> dict | None:
        """Decide which existing tool (if any) to reuse.

        Two reliable stages instead of one flaky id-picker:
          1. Deterministic short-circuit: identical request (whitespace/case
             normalized) → reuse that exact tool, zero model calls.
          2. Parameterized tools: classify the request into one of the existing
             *tool_type* labels (a small single-choice task the local model
             handles far more reliably than picking an id from a noisy catalog).
             Legacy hardcoded tools (no param_schema) are only reused via stage 1,
             since running them with different numbers would give a wrong answer.
        """
        entries = self._load_manifest()
        if not entries:
            return None

        norm_req = _normalize_request(request)
        for e in entries:
            if _normalize_request(e.get("request", "")) == norm_req:
                logger.info("dynamic_tools: exact-match reuse slug=%s", e.get("slug"))
                return e

        param_entries = [e for e in entries if e.get("param_schema") and e.get("tool_type")]
        if not param_entries:
            return None
        # One representative (newest) per tool_type, carrying an example request so
        # the classifier matches on *function*, not on a fragile label that drifts
        # between runs (e.g. "loan_calculator" vs "房貸等額本息月供計算").
        rep: dict[str, dict] = {}
        for e in sorted(param_entries, key=lambda x: x.get("created_at", "")):
            rep[e["tool_type"]] = e  # later (newer) wins
        candidates = [(t, _first_line(e.get("request", ""), 60)) for t, e in rep.items()]
        choice = self._classify_tool_type(request, candidates)
        if not choice:
            return None
        matches = [e for e in param_entries if e["tool_type"] == choice]
        if not matches:
            return None
        return sorted(matches, key=lambda e: e.get("created_at", ""))[-1]

    def _classify_tool_type(self, request: str, candidates: list[tuple[str, str]]) -> str | None:
        listing = "\n".join(
            f"{i + 1}. {t}（例如：{ex}）" for i, (t, ex) in enumerate(candidates)
        )
        prompt = (
            "判斷下面的需求屬於哪一個既有『工具類型』。只有當需求的『功能』與某類型一致時"
            "才選它（具體數值/城市/標的不同沒關係，會自動帶入新值）；用每項的範例判斷它的功能。"
            "若都不符合就回 NEW。\n"
            f"工具類型清單：\n{listing}\n\n需求：{request}\n\n"
            "只回覆其中一個類型的完整名稱，或回 NEW。不要加任何解說。"
        )
        try:
            ans = self.client.generate(prompt, temperature=0.0).strip()
        except Exception:
            return None
        ans = ans.splitlines()[0].strip() if ans else ""
        if not ans or ans.upper() == "NEW":
            return None
        types = [t for t, _ in candidates]
        for t in types:
            if ans == t or t in ans or ans in t:
                return t
        return None

    # ── distillation ────────────────────────────────────────────────────────

    def _retrieve_knowledge(self, request: str) -> list:
        if self.knowledge_db is None:
            return []
        try:
            return self.knowledge_db.retrieve_codegen_knowledge(request, k=6)
        except Exception:
            logger.exception("dynamic_tools: retrieve_codegen_knowledge failed")
            return []

    def _mark_knowledge_applied(self, rows: list) -> None:
        if self.knowledge_db is None or not rows:
            return
        try:
            self.knowledge_db.mark_codegen_applied(tuple(r.knowledge_id for r in rows))
        except Exception:
            logger.exception("dynamic_tools: mark_codegen_applied failed")

    def _distill_failure(self, request: str, final_code: str, last_error: str) -> None:
        """After a task needed repairs, ask the model to abstract the fix into a
        general, transferable rule and store it (origin='distilled', low conf)."""
        if self.knowledge_db is None:
            return
        prompt = (
            "剛才一個寫程式任務在多次嘗試後才成功。請把『這次學到的教訓』抽象成"
            "一條**通用、可遷移**的寫程式規則（不要綁特定標的/網址/欄位名）。\n"
            "只輸出 JSON：{\"category\": \"data_fetch|numeric_method|parsing|validation|output_contract|finance\", "
            "\"title\": \"短標題\", \"technique\": \"一兩句通則\", \"keywords\": [\"關鍵字\"]}。\n\n"
            f"需求：{request}\n曾遇到的錯誤：\n{_tail(last_error, 500)}\n"
        )
        try:
            raw = self.client.generate(prompt, temperature=0.0)
            data = _load_json_object(raw)
            if not data:
                return
            self.knowledge_db.upsert_codegen_knowledge(
                category=str(data.get("category", "validation")),
                title=str(data.get("title", "")).strip(),
                technique=str(data.get("technique", "")).strip(),
                keywords=tuple(str(k) for k in data.get("keywords", []) if str(k).strip()),
                origin="distilled",
                confidence=0.4,
            )
            logger.info("dynamic_tools: distilled rule '%s'", data.get("title"))
        except Exception:
            logger.exception("dynamic_tools: distill_failure failed")

    # ── misc ────────────────────────────────────────────────────────────────

    def _make_slug(self, request: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "_", request.lower()).strip("_")[:24] or "tool"
        suffix = sha1(f"{request}|{_utc_now_iso()}".encode("utf-8")).hexdigest()[:8]
        return f"{base}_{suffix}"


# ── module helpers ───────────────────────────────────────────────────────────


_REQUIRES_STOPWORDS = frozenset({
    "none", "n/a", "na", "stdlib", "standard", "builtin", "builtins",
    "無", "無需", "標準函式庫", "標準庫", "nothing", "no",
})
_STDLIB_MODULES = frozenset({
    "os", "sys", "re", "json", "math", "datetime", "time", "urllib", "urllib3",
    "http", "collections", "itertools", "functools", "statistics", "decimal",
    "csv", "io", "pathlib", "typing", "argparse", "subprocess", "random",
    "hashlib", "base64", "ssl", "socket", "logging",
})


_APPROVED_PACKAGES: frozenset[str] = frozenset({
    # data / HTTP / HTML
    "yfinance", "requests", "httpx", "beautifulsoup4", "bs4", "lxml", "html5lib",
    # numerical / display
    "pandas", "numpy", "tabulate", "tqdm", "pillow",
    # time / util
    "python-dateutil", "pytz", "tzdata",
})


def _is_safe_pkg(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-\[\]=<>]*", name))


def _is_approved_pkg(name: str) -> bool:
    base = re.split(r"[><=!\[]", name)[0].lower().strip().replace("-", "_").replace(".", "_")
    canon = re.split(r"[><=!\[]", name)[0].lower().strip()
    return canon in _APPROVED_PACKAGES or base in {
        p.lower().replace("-", "_").replace(".", "_") for p in _APPROVED_PACKAGES
    }


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


def _tail(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _first_line(text: str, limit: int) -> str:
    line = (text or "").strip().splitlines()[0] if text.strip() else ""
    return line[:limit]


def build_dynamic_tool_runner_from_settings(settings) -> DynamicToolRunner | None:
    """Build a runner from AssistantSettings, or None when no usable local text
    model / non-ollama backend (mirrors natural_language.py builder style)."""
    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    if backend != "ollama":
        if backend:
            logger.warning("dynamic_tools: unsupported backend=%s", backend)
        return None
    strong_model = _select_model(settings.openclaw_local_text_model)
    if not strong_model:
        return None
    # Fast tier-1 model (code-specialized). Falls back to the strong model when
    # unset, collapsing the cascade to single-tier (old) behavior.
    fast_model = (getattr(settings, "openclaw_codegen_fast_model", None) or "").strip() or strong_model

    # Codegen needs more time + context than the NL router.
    # num_ctx=8192: prevents 4096-default from leaving too few tokens for response.
    # num_predict=1000: caps Phase-1 at ~4KB (~667s at 1.5 tok/s) safely under timeout.
    #   1000 tokens is enough for any single-purpose script (Black-Scholes ~300 tok).
    endpoint = settings.openclaw_local_text_endpoint
    if not probe_ollama(endpoint):
        logger.warning(
            "dynamic_tools: Ollama not reachable at %s — /new will fail until it comes up",
            endpoint,
        )

    codegen_timeout = max(900, settings.openclaw_local_text_timeout_seconds * 12)
    client = OllamaTextClient(
        endpoint=endpoint,
        model=fast_model,
        timeout_seconds=codegen_timeout,
        num_ctx=8192,
        num_predict=1000,
    )

    knowledge_db = None
    try:
        from .knowledge_db import KnowledgeDatabase

        knowledge_db = KnowledgeDatabase(settings.knowledge_db_path)
        knowledge_db.seed_codegen_knowledge()
    except Exception:
        logger.exception("dynamic_tools: knowledge DB init failed; continuing without RAG")

    tools_dir = _resolve_tools_dir()
    return DynamicToolRunner(
        client=client, tools_dir=tools_dir, knowledge_db=knowledge_db,
        fast_model=fast_model, strong_model=strong_model,
    )


def _select_model(raw_models: str | None) -> str | None:
    if not raw_models:
        return None
    candidates = [p.strip() for p in raw_models.split(",") if p.strip()]
    if not candidates:
        return None
    # pick the largest by :<n>b tag (strongest), else first.
    def size(model: str) -> float:
        m = re.search(r":(\d+(?:\.\d+)?)b\b", model.lower())
        return float(m.group(1)) if m else 0.0

    return max(candidates, key=size)


def _resolve_tools_dir() -> Path:
    # generated_tools/ at the aka_no_claw repo root (two levels up from this file:
    # src/openclaw_adapter/dynamic_tools.py -> repo root).
    return Path(__file__).resolve().parents[2] / "generated_tools"


# ── benchmarks / selftest ────────────────────────────────────────────────────

_BENCHMARKS_PATH = Path(__file__).resolve().parent / "dynamic_tools_benchmarks.json"
_NUM_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")
_PCT_NUM_RE = re.compile(r"(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*%")


def _numbers(text: str, *, pct_only: bool) -> list[float]:
    raw = (_PCT_NUM_RE if pct_only else _NUM_RE).findall(text or "")
    out: list[float] = []
    for token in raw:
        try:
            out.append(float(token.replace(",", "")))
        except ValueError:
            continue
    return out


def _check_numeric(answer: str, check: dict) -> tuple[bool, str]:
    label = check.get("label", "數值")
    expected = float(check["expected"])
    tol = abs(expected) * float(check.get("tolerance_pct", 5.0)) / 100.0
    tol = max(tol, 1e-9)
    pool = _numbers(answer, pct_only=bool(check.get("is_pct")))
    best = None
    for num in pool:
        diff = abs(num - expected)
        if best is None or diff < best[0]:
            best = (diff, num)
    if best is not None and best[0] <= tol:
        return True, f"{label}: 命中 {best[1]:g}（期望 {expected:g}±{tol:g}）"
    got = f"最接近 {best[1]:g}" if best else "找不到數值"
    return False, f"{label}: 失敗（期望 {expected:g}±{tol:g}，{got}）"


def _check_direction(answer: str, keyword_groups: list) -> tuple[bool, str]:
    lc = (answer or "").lower()
    for group in keyword_groups:
        if not any(str(kw).lower() in lc for kw in group):
            return False, f"方向性失敗：缺少 {group} 任一關鍵字"
    return True, "方向性通過"


def load_benchmarks() -> list[dict]:
    return json.loads(_BENCHMARKS_PATH.read_text(encoding="utf-8"))


def run_benchmarks(runner: DynamicToolRunner, benchmarks: list[dict] | None = None) -> bool:
    benchmarks = benchmarks if benchmarks is not None else load_benchmarks()
    all_pass = True
    for bench in benchmarks:
        print(f"\n=== benchmark {bench['id']}: {bench['request']} ===")
        result = runner.run_detailed(bench["request"])
        print(f"ok={result.ok} reused={result.reused} gens={result.generations}")
        print("ANSWER:", result.answer or result.error)
        bench_pass = result.ok
        if not result.ok:
            all_pass = False
            print("FAIL: 工具執行失敗")
            continue
        for check in bench.get("numeric_checks", []):
            ok, msg = _check_numeric(result.answer, check)
            bench_pass = bench_pass and ok
            print(("  ✅ " if ok else "  ❌ ") + msg)
        if bench.get("direction_keywords"):
            ok, msg = _check_direction(result.answer, bench["direction_keywords"])
            bench_pass = bench_pass and ok
            print(("  ✅ " if ok else "  ❌ ") + msg)
        print(f"  → {bench['id']}: {'PASS' if bench_pass else 'FAIL'}")
        all_pass = all_pass and bench_pass
    print(f"\n=== overall: {'PASS' if all_pass else 'FAIL'} ===")
    return all_pass


def _selftest_main() -> int:
    from assistant_runtime import get_settings, load_dotenv

    load_dotenv()
    settings = get_settings()
    runner = build_dynamic_tool_runner_from_settings(settings)
    if runner is None:
        print("dynamic tools 未啟用（無本地 text model / 非 ollama backend）。")
        return 2
    return 0 if run_benchmarks(runner) else 1


if __name__ == "__main__":
    import sys as _sys

    if len(_sys.argv) > 1 and _sys.argv[1] == "selftest":
        raise SystemExit(_selftest_main())
    print("用法：python -m openclaw_adapter.dynamic_tools selftest")
    raise SystemExit(0)
