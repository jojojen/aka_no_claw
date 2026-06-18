"""Dynamic self-writing tools for the ``/new`` Telegram command.

When a request isn't covered by a fixed tool, ``DynamicToolRunner`` asks a text
generation backend to WRITE a single-file Python tool, runs it under a
lightweight guardrail, and returns the answer. Default codegen uses local
Ollama with a model-tier cascade: a fast code-specialized model writes first,
escalating to the stronger model only on repeated failure. Optionally,
``OPENCLAW_CODEGEN_BACKEND=opencode`` routes generation/repair/validation to
OpenCode Big Pickle while keeping OpenClaw's execution guardrails. Tools persist in
a gitignored ``generated_tools/`` folder (+ ``manifest.json``) so similar
requests can be reused instead of regenerated.

Default mode is local / free — no paid frontier API.

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
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_OLLAMA_MAX_RETRIES = 3       # transient-error retries in generate()
_OLLAMA_RETRY_BASE_SEC = 1.0  # first backoff; doubles each attempt (1, 2, 4 s)
_OPENCODE_MAX_RETRIES = 3
_OPENCODE_RETRY_BASE_SEC = 1.0

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
# Knowledge rules cite formula/definition pages as `參考: <url>` instead of
# hardcoding domain formulas in the DB; the pages are fetched and distilled
# per-request (_ground_references).
_REF_URL_RE = re.compile(r"參考:\s*(https?://[^\s）)」』]+)")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
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
    # Output cut between a try block and its handler — the classic shape of a
    # num_predict-capped script (models put try/except near the end).
    "expected 'except'",
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
    # Bare `import urllib` does NOT expose urllib.request/parse — the usual way
    # generated code uses it — so import the submodules explicitly.
    submodule_imports = {"urllib": "import urllib.request, urllib.parse, urllib.error"}
    header = "".join(submodule_imports.get(m, f"import {m}") + "\n" for m in missing)
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


class TextGenerationClient(Protocol):
    model: str
    timeout_seconds: int
    num_ctx: int | None
    num_predict: int | None

    def generate(self, prompt: str, *, temperature: float = 0.0,
                 think: bool = False) -> str:
        ...


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


def probe_opencode(
    base_url: str,
    *,
    model: str = "big-pickle",
    api_key: str | None = None,
    timeout: float = 5.0,
) -> bool:
    """Return True when the OpenCode OpenAI-compatible completions endpoint works."""
    client = OpenCodeTextClient(
        base_url=base_url,
        model=model,
        timeout_seconds=max(1, int(timeout)),
        api_key=api_key,
        max_tokens=4,
    )
    try:
        client.generate("Reply with: ok", temperature=0.0)
        return True
    except Exception as exc:
        logger.warning("dynamic_tools: OpenCode probe failed: %s", exc)
        return False


def probe_opencode_cli(
    *,
    model: str = "opencode/big-pickle",
    timeout: float = 20.0,
) -> bool:
    """Return True when the opencode CLI can produce one non-empty response."""
    if not shutil.which("opencode"):
        return False
    client = OpenCodeCliTextClient(model=model, timeout_seconds=max(1, int(timeout)))
    try:
        return bool(client.generate("Only output exactly: ok", temperature=0.0).strip())
    except Exception as exc:
        logger.warning("dynamic_tools: OpenCode CLI probe failed: %s", exc)
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


class OpenCodeTextClient:
    """OpenCode Zen OpenAI-compatible completions client for Big Pickle."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str = "big-pickle",
        timeout_seconds: int = 900,
        api_key: str | None = None,
        max_tokens: int | None = 32000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = max(1, timeout_seconds)
        self.api_key = api_key
        # DynamicToolRunner mutates these attributes across tiers. OpenCode does
        # not use num_ctx and maps num_predict to max_tokens when present.
        self.num_ctx: int | None = None
        self.num_predict: int | None = max_tokens

    def _url(self) -> str:
        if self.base_url.endswith("/completions"):
            return self.base_url
        return f"{self.base_url}/completions"

    def generate(self, prompt: str, *, temperature: float = 0.0, think: bool = False) -> str:
        payload: dict[str, object] = {
            "model": self.model,
            "prompt": prompt,
            "temperature": temperature,
            "stream": False,
        }
        if self.num_predict is not None:
            payload["max_tokens"] = self.num_predict
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self._url(),
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        last_exc: RuntimeError | None = None
        body = ""
        for attempt in range(1, _OPENCODE_MAX_RETRIES + 1):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read().decode("utf-8", errors="replace")
                last_exc = None
                break
            except HTTPError as exc:
                if exc.code < 500:
                    detail = ""
                    try:
                        detail = exc.read().decode("utf-8", errors="replace")[:400]
                    except Exception:
                        detail = ""
                    raise RuntimeError(f"OpenCode HTTP {exc.code}: {detail}") from exc
                last_exc = RuntimeError(f"OpenCode HTTP {exc.code}")
            except URLError as exc:
                last_exc = RuntimeError(f"OpenCode request failed: {exc.reason}")
            if attempt < _OPENCODE_MAX_RETRIES:
                delay = _OPENCODE_RETRY_BASE_SEC * (2.0 ** (attempt - 1))
                logger.warning(
                    "OpenCode transient error attempt %d/%d; retrying in %.0fs: %s",
                    attempt, _OPENCODE_MAX_RETRIES, delay, last_exc,
                )
                time.sleep(delay)
        if last_exc is not None:
            raise RuntimeError(
                f"OpenCode 無回應（已重試 {_OPENCODE_MAX_RETRIES} 次）"
            ) from last_exc
        parsed = json.loads(body)
        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("OpenCode response missing choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise RuntimeError("OpenCode choice was not an object")
        text = first.get("text")
        if text is None and isinstance(first.get("message"), dict):
            text = first["message"].get("content")
        if not isinstance(text, str):
            raise RuntimeError(f"OpenCode response text type was {type(text).__name__}")
        return _THINK_RE.sub("", text).strip()


class OpenCodeCliTextClient:
    """Fallback transport through ``opencode run`` when direct HTTP is blocked."""

    def __init__(
        self,
        *,
        model: str = "opencode/big-pickle",
        timeout_seconds: int = 900,
        cwd: str | Path | None = None,
    ) -> None:
        self.model = model
        self.timeout_seconds = max(1, timeout_seconds)
        self.cwd = str(cwd or tempfile.gettempdir())
        self.home_dir = str(Path(self.cwd) / ".opencode-home")
        self.num_ctx: int | None = None
        self.num_predict: int | None = None

    def generate(self, prompt: str, *, temperature: float = 0.0, think: bool = False) -> str:
        Path(self.home_dir).mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        # Keep opencode's config from the real XDG config dir, but isolate HOME
        # so Claude/Codex global memories like ~/.claude/CLAUDE.md cannot leak
        # into generated tool answers.
        env.setdefault("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        env["HOME"] = self.home_dir
        env["XDG_DATA_HOME"] = str(Path(self.home_dir) / ".local" / "share")
        env["XDG_CACHE_HOME"] = str(Path(self.home_dir) / ".cache")
        env["CLAUDE_CONFIG_DIR"] = str(Path(self.home_dir) / ".claude")
        cmd = [
            "opencode", "run", "--pure", "-m", self.model,
            "--dir", self.cwd, prompt,
        ]
        try:
            proc = subprocess.run(
                cmd,
                shell=False,
                cwd=self.cwd,
                timeout=self.timeout_seconds,
                capture_output=True,
                text=True,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"OpenCode CLI timeout >{self.timeout_seconds}s") from exc
        if proc.returncode != 0:
            detail = _tail((proc.stderr or proc.stdout or "").strip(), 800)
            raise RuntimeError(f"OpenCode CLI failed: {detail}")
        text = _ANSI_RE.sub("", proc.stdout or "")
        lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("> build") or stripped.startswith("> "):
                continue
            lines.append(line)
        return _THINK_RE.sub("", "\n".join(lines)).strip()


class DynamicToolRunner:
    def __init__(
        self,
        *,
        client: TextGenerationClient,
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
        # Raw text of fetched `參考:` pages, keyed by URL (per-process politeness cache).
        self._reference_cache: dict[str, str] = {}
        # Search-grounding fallback reuses /search's Yahoo backend; injectable for
        # tests. Hard budget: the user's IP must never get banned, so at most
        # search_daily_cap queries/day, persisted in search_state_path (which
        # also caches distilled results so re-runs cost zero queries).
        self.search_fn = None  # (query, max_results) -> Sequence[WebSearchResult]
        self.search_daily_cap = 4
        self.search_state_path = Path(tools_dir) / "search_state.json"
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
        if result.generations == 0:
            return f"⚠️ 無法完成\n{result.error}"
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
            '{"core": "<要查什麼資料、要做什麼計算的精簡描述，去掉任何排版/格式/範例指示>", '
            '"format": "<使用者指定的輸出格式或範例原文；沒有就空字串>"}\n'
            "規則：core 必須完整保留『要查的資料種類與標的』（例如城市、股票、日期）"
            "以及『要做的計算與其全部條件』（例如複利、本金、年數、百分比口徑）——"
            "計算需求屬於 core，絕不可被刪掉或移進 format；"
            "core 不可包含任何『格式如下』『版型』『emoji 範例』等排版指示；"
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
            valid, reason = self._validate_answer(request, result.answer)
            if not valid:
                logger.info(
                    "dynamic_tools: reused answer failed validation (%s) slug=%s",
                    reason, slug,
                )
                return None
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
        always_on, topical = self._load_rules_split()
        feasible, why, selected = self._preflight(request, topical)
        if not feasible:
            logger.info("dynamic_tools: infeasible request, refusing honestly: %s", why)
            return DynamicToolResult(
                ok=False, generations=0,
                error=f"此需求需要的資料沒有免金鑰的公開資料源，無法以生成工具取得：{why}",
            )
        if selected is None:
            knowledge_rows = (
                self._keyword_fallback_rules(request, always_on)
                if self.knowledge_db is not None else []
            )
            has_recipe = any("*" not in r.keywords for r in knowledge_rows)
        else:
            if selected:
                selected = self._merge_keyword_topicals(request, selected)
            # Topical recipes lead the methodology block — burying them after a
            # dozen always-on disciplines made small models miss the recipe.
            knowledge_rows = selected + always_on
            has_recipe = bool(selected)
            if selected:
                logger.info("dynamic_tools: preflight selected rules=%s",
                            [r.title for r in selected])

        # Grounding first: if a reference page (rule URL or search fallback)
        # already supplies the needed fact, the explorer must see it — otherwise
        # it hallucinates endpoints for data we already hold.
        references = self._ground_references(request, knowledge_rows)
        if references is None:
            references = self._search_ground(request)
        elif self._needs_search_grounding(request, references):
            extra = self._search_ground(request)
            if extra:
                references = references + "\n" + extra

        # Phase 0: live API exploration only for domains without a stored recipe —
        # a selected recipe already carries a verified endpoint + field structure,
        # so the discovery round-trip (one more LLM call + one HTTP run) is waste.
        api_structure = None if has_recipe else self._explore_api(
            request, knowledge_rows, references=references)

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
                                           api_structure=api_structure,
                                           references=references)
                code = self._pass_syntax_gate(request, code, knowledge_rows,
                                              think=think, api_structure=api_structure,
                                              references=references)
                phase_gen = 1
                while True:
                    generations += 1
                    tool_path.write_text(code, encoding="utf-8")
                    exec_result = self._install_and_execute(slug, tool_path, code)
                    last_stdout = exec_result.raw_stdout
                    if exec_result.ok:
                        valid, reason = self._validate_answer(
                            request, exec_result.answer, references=references)
                        if valid:
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
                        last_error = (
                            "答案驗證未通過（程式執行成功但輸出內容不符需求）：" + reason
                            + "\n（修正方向：改程式邏輯，讓輸出的依據取自真實資料點、"
                            "且用依據能重算出答案。驗證訊息裡出現的數字只是檢查線索，"
                            "絕不可把它寫死進程式或拿來 assert 對照。）"
                        )
                        logger.info(
                            "dynamic_tools: answer validation FAIL slug=%s reason=%s",
                            slug, reason,
                        )
                    else:
                        last_error = exec_result.error
                        logger.info("dynamic_tools: exec failed slug=%s err=%s",
                                    slug, _tail(last_error, 200))
                    if phase_gen >= phase_max:
                        break
                    phase_gen += 1
                    prev_code = code
                    code = self._repair_code(request, code, last_error, knowledge_rows,
                                             think=think, api_structure=api_structure,
                                             references=references)
                    if code.strip() == prev_code.strip():
                        # The model returned the same code — re-executing it will
                        # fail identically; skip straight to the next tier.
                        logger.info("dynamic_tools: repair produced identical code, "
                                    "escalating early (phase=%s)", phase)
                        break
                    code = self._pass_syntax_gate(request, code, knowledge_rows,
                                                  think=think, api_structure=api_structure,
                                                  references=references)

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
        think: bool, api_structure: str | None, references: str | None = None,
    ) -> str:
        """Lever 2: ast.parse the code before paying for a subprocess run.

        Syntax-only repairs don't count as a generation. Lever 1: if the failure
        looks like truncation (output cut mid-statement) and num_predict is
        still capped, bump it and regenerate from scratch instead of trying to
        "repair" half-written code — repairing a truncated script regenerates
        at the same cap and truncates again, burning the whole tier's budget.
        """
        for _ in range(_MAX_SYNTAX_FIXES):
            err = _syntax_error(code)
            if not err:
                # Code parses; deterministically repair missing stdlib imports
                # (a runtime NameError ast.parse can't see) before execution.
                return _ensure_stdlib_imports(code)
            logger.info("dynamic_tools: syntax gate caught: %s", err)
            if (_is_truncation_error(err)
                    and self.client.num_predict is not None
                    and self.client.num_predict < _NUM_PREDICT_BUMP):
                logger.info(
                    "dynamic_tools: truncation detected, bumping num_predict %s->%s and regenerating",
                    self.client.num_predict, _NUM_PREDICT_BUMP,
                )
                self.client.num_predict = _NUM_PREDICT_BUMP
                code = self._generate_code(request, knowledge_rows, think=think,
                                           api_structure=api_structure,
                                           references=references)
                continue
            code = self._repair_code(request, code, f"SyntaxError: {err}", knowledge_rows,
                                     think=think, api_structure=api_structure,
                                     references=references)
        return code

    def _install_and_execute(self, slug: str, tool_path: Path, code: str) -> DynamicToolResult:
        """Install declared requires, execute, with one extra retry purely for an
        auto-installable ModuleNotFoundError (doesn't count as a generation)."""
        requires = self._parse_requires(code)
        if requires:
            try:
                self._pip_install(requires)
            except RuntimeError as exc:
                # Unapproved package is a *code* problem (the script chose the
                # wrong dependency), so it must feed the repair loop instead of
                # crashing /new.
                return DynamicToolResult(
                    ok=False, slug=slug,
                    error=f"{exc}\n請改用標準函式庫（或核准清單內的套件）重寫，不要依賴該套件。",
                )
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
                # Import name ≠ pip distribution name (dateutil→python-dateutil):
                # installing the raw module name gets an approved package blocked.
                pkg = _MODULE_TO_PIP.get(pkg, pkg)
                logger.info("dynamic_tools: auto-installing missing module=%s", pkg)
                try:
                    self._pip_install((pkg,))
                except RuntimeError as exc:
                    return DynamicToolResult(
                        ok=False, slug=slug, raw_stdout=stdout,
                        error=f"{exc}\n請改用標準函式庫（或核准清單內的套件）重寫，不要依賴該套件。",
                    )
                continue
            return DynamicToolResult(
                ok=False, slug=slug, raw_stdout=stdout,
                error=_tail(stderr, 600) or f"非零退出碼 {proc.returncode}",
            )
        # exhausted auto-install retry
        return DynamicToolResult(ok=False, slug=slug, error="缺少套件且自動安裝後仍失敗。")

    # macOS sandbox-exec profile: deny writes to /Users except tool dir,
    # allow everything else (network access needed for tools that fetch data).
    _SANDBOX_PROFILE_TEMPLATE = """\
(version 1)
(allow default)
(deny file-write* (subpath "/Users"))
(allow file-write* (subpath "{tool_dir}"))
"""

    def _execute(self, tool_path: Path) -> subprocess.CompletedProcess:
        venv_python = self._ensure_venv()
        tool_dir = tool_path.parent
        env = self._clean_env(tool_dir)
        base_cmd: list[str] = [str(venv_python), str(tool_path)]
        cmd = list(base_cmd)
        # Wrap with sandbox-exec on macOS if available (SEC-4).
        import shutil
        used_sandbox = False
        if shutil.which("sandbox-exec"):
            profile = self._SANDBOX_PROFILE_TEMPLATE.format(tool_dir=str(tool_dir))
            cmd = ["sandbox-exec", "-p", profile, *cmd]
            used_sandbox = True
        try:
            proc = subprocess.run(
                cmd,
                shell=False,
                cwd=str(tool_dir),
                timeout=self.exec_timeout_seconds,
                capture_output=True,
                text=True,
                env=env,
            )
            if used_sandbox and proc.returncode != 0 and _sandbox_wrapper_failed(proc.stderr or ""):
                logger.warning(
                    "dynamic_tools: sandbox-exec unavailable at runtime; retrying without wrapper"
                )
                proc = subprocess.run(
                    base_cmd,
                    shell=False,
                    cwd=str(tool_dir),
                    timeout=self.exec_timeout_seconds,
                    capture_output=True,
                    text=True,
                    env=env,
                )
            return proc
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
                out.append(_MODULE_TO_PIP.get(token, token))
        # de-dup, preserve order
        seen: set[str] = set()
        return tuple(p for p in out if not (p in seen or seen.add(p)))

    # ── model prompts ───────────────────────────────────────────────────────

    def _fetch_url_text(self, url: str) -> str:
        """Reference-page fetch via the same readable-text extractor /fetch uses."""
        cached = self._reference_cache.get(url)
        if cached is not None:
            return cached
        from .web_search import fetch_page_text
        text = fetch_page_text(url, timeout_seconds=15, max_chars=8000,
                               user_agent="Mozilla/5.0")
        if text:  # don't cache failures — they may be transient
            self._reference_cache[url] = text
        return text

    def _ground_references(self, request: str, knowledge_rows: list) -> str | None:
        """Domain formulas/conventions are NOT hardcoded in the knowledge DB —
        rules cite `參考: <url>` pages instead. Fetch them and have the fast
        model distill the request-relevant definitions; fail open to None."""
        urls: list[str] = []
        for row in knowledge_rows:
            for url in _REF_URL_RE.findall(getattr(row, "technique", "")):
                if url not in urls:
                    urls.append(url)
        if not urls:
            return None
        texts: list[str] = []
        for url in urls[:2]:
            try:
                text = self._fetch_url_text(url)
            except Exception as exc:
                logger.info("dynamic_tools: reference fetch failed url=%s err=%s", url, exc)
                continue
            if text:
                texts.append(text)
            else:
                logger.info("dynamic_tools: reference fetch empty url=%s", url)
        if not texts:
            return None
        extract = self._distill_reference_texts(request, texts)
        if extract is None:
            return None
        logger.info("dynamic_tools: grounded %d reference page(s) -> %d chars: %s",
                    len(texts), len(extract), _tail(extract, 200))
        return extract

    def _distill_reference_texts(self, request: str, texts: list[str]) -> str | None:
        saved_model = self.client.model
        saved_ctx = self.client.num_ctx
        extracts: list[str] = []
        try:
            # Needle-in-noise extraction (one key sentence inside pages of table
            # junk) is where the fast model drops the value; use the strong one.
            self.client.model = self.strong_model
            # One page per call, with a window that actually fits: a fetched
            # page can be ~8000 CJK chars ≈ more tokens than the default 8192
            # num_ctx. An overflowing prompt gets head-truncated by Ollama —
            # the instructions silently vanish and the model answers NONE.
            if saved_ctx is not None:
                self.client.num_ctx = max(saved_ctx, 16384)
            for text in texts:
                prompt = (
                    "從下面參考資料中，抽取與需求直接相關的公式、名詞定義與慣例"
                    "（例如期間如何界定、期初值取哪個時點的值、百分比如何呈現；"
                    "事實查詢就抽取現值、生效/發布日期與出處）。\n"
                    "⚠️ 若資料同時提到『現行值』與『檢討中/提案中/預期中的未來值』，"
                    "兩者必須分開標明（格式：現行值 X（自某時點）；另有檢討中的 Y，尚未生效），"
                    "絕不可把尚未生效的值當成現值；資料裡『現在、目前』後面接的數字才是現值。\n"
                    "⚠️ 名詞必須嚴格一致：需求點名的指標/名詞，與資料實際描述的指標必須是同一個；"
                    "同領域但不同名詞的指標（別的利率、別的費率、別的統計）不算相關，"
                    "這種資料就輸出 NONE，絕不可拿相近指標的數值充當。\n"
                    f"需求：{request}\n\n"
                    "參考資料：\n" + text + "\n\n"
                    "只輸出條列要點（公式用一行文字描述），200 字內；"
                    "參考資料與需求無關就只輸出 NONE。"
                )
                try:
                    extract = self.client.generate(
                        prompt, temperature=0.0, think=False).strip()
                except Exception as exc:
                    logger.info("dynamic_tools: reference distillation failed: %s", exc)
                    continue
                if not extract or extract.upper().startswith("NONE"):
                    continue
                # Models sometimes wrap the NONE verdict in a bullet
                # ("- **X**：NONE") instead of answering bare NONE; lines
                # carrying it are junk, and an extract that is ONLY such lines
                # grounded nothing.
                lines = [l for l in extract.splitlines()
                         if l.strip() and "NONE" not in l.upper()]
                if lines:
                    extracts.append("\n".join(lines))
        finally:
            self.client.model = saved_model
            self.client.num_ctx = saved_ctx
        if not extracts:
            return None
        return "\n".join(extracts)

    # ── search-grounding fallback (/search + /fetch reuse) ──────────────────

    def _load_search_state(self) -> dict:
        try:
            state = json.loads(self.search_state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError("not a dict")
        except Exception:
            state = {}
        today = date.today().isoformat()
        if state.get("date") != today:
            state = {"date": today, "count": 0, "cache": {}}
        state.setdefault("count", 0)
        state.setdefault("cache", {})
        return state

    def _save_search_state(self, state: dict) -> None:
        try:
            self.search_state_path.write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except Exception:
            logger.exception("dynamic_tools: search state save failed")

    def _needs_search_grounding(self, request: str, references: str) -> bool:
        """Rule grounding can 'succeed' with a generic formula page while the
        request still hinges on a CURRENT institution-announced value (policy
        rate, official fee, latest decision) that has no key-free API and may be
        stale in training data. One cheap judgment decides whether to spend a
        search query on it. Fail-closed: errors return False (never burn budget
        on a sick gate)."""
        prompt = (
            "判斷要完成下面的需求，是否還缺少『當下時點的機構公告型事實數值』"
            "（例如現行政策利率、官方費率、最新會議決議值——這類值沒有免金鑰公開 API 可查，"
            "且模型訓練資料裡的舊值可能已過期），而且下面的參考資料也沒有給出該數值。\n"
            "股價、匯率、天氣等有免金鑰公開 API 可即時查到的資料不算缺。\n"
            "判斷要點：需求文字若點名了某機構的『現在/目前/最新』數值"
            "（如『現在的政策金利』『目前的基準費率』），而參考資料只有公式/定義、"
            "沒有給出該數值的具體現值與時點，就是 YES。\n"
            f"需求：{request}\n"
            f"參考資料：\n{_tail(references, 600)}\n"
            "只輸出一行：YES（缺，需要補搜尋）或 NO。"
        )
        saved_model = self.client.model
        try:
            # Judgment call, not codegen: the fast model rubber-stamps NO even
            # when the request literally names an announced value, so this gate
            # runs on the strong model (one short call, only after rule
            # grounding succeeded).
            self.client.model = self.strong_model
            ans = self.client.generate(prompt, temperature=0.0, think=False).strip()
        except Exception:
            logger.exception("dynamic_tools: search-grounding gate failed; assuming NO")
            return False
        finally:
            self.client.model = saved_model
        need = ans.upper().startswith("YES")
        logger.info("dynamic_tools: search-grounding gate verdict=%s (%s)",
                    "YES" if need else "NO", _tail(ans, 120))
        return need

    def _search_ground(self, request: str) -> str | None:
        """When no rule reference page covers the request, fall back to ONE web
        search (same Yahoo backend as /search) + page fetches (same extractor
        as /fetch), distilled like rule grounding. Hard daily budget + per-day
        result cache keep query volume near zero; everything fails open."""
        if self.search_fn is None:
            # Not wired (unit tests / minimal configs): never reach the network.
            return None
        try:
            state = self._load_search_state()
            cached = state["cache"].get(request)
            if cached is not None:
                logger.info("dynamic_tools: search grounding cache hit for request")
                if isinstance(cached, str):  # legacy entries cached the distilled block
                    return cached or None
                texts = list(cached.get("texts") or [])
                sources = list(cached.get("sources") or [])
                if not texts:
                    return None
                # Raw page texts are cached (not the distillate) so distillation
                # improvements take effect on re-runs without a new query.
                extract = self._distill_reference_texts(request, texts)
                if not extract:
                    return None
                return extract + "\n來源:\n" + "\n".join(sources)
            if state["count"] >= self.search_daily_cap:
                logger.info("dynamic_tools: search grounding budget exhausted (%d/%d) — skipping",
                            state["count"], self.search_daily_cap)
                return None
            saved_model = self.client.model
            try:
                self.client.model = self.fast_model
                query = self.client.generate(
                    "把下面的需求轉成一條適合網頁搜尋引擎的網頁搜尋查詢"
                    "（保留關鍵專有名詞，可用中文或日文）。\n"
                    f"需求：{request}\n"
                    "只輸出查詢字串一行，不要引號、不要解說。",
                    temperature=0.0, think=False,
                ).strip().splitlines()[0].strip()
            finally:
                self.client.model = saved_model
            if not query:
                return None
            search = self.search_fn
            # Count the query BEFORE issuing it: a crash after the request has
            # hit Yahoo must still burn budget.
            state["count"] += 1
            self._save_search_state(state)
            logger.info("dynamic_tools: search grounding query=%r (budget %d/%d)",
                        query, state["count"], self.search_daily_cap)
            results = list(search(query, 4) or [])
            logger.info("dynamic_tools: search grounding got %d result(s): %s",
                        len(results), [getattr(r, "url", "") for r in results])
            texts: list[str] = []
            sources: list[str] = []
            for res in results:
                if len(texts) >= 2:
                    break
                url = getattr(res, "url", "")
                if not url:
                    continue
                parsed = urlparse(url)
                # The extractor is HTML-only and engine-internal links (e.g.
                # search.<engine>/image/...) carry no content — fetching them
                # wastes the 2-page budget on garbage the distiller rejects.
                if parsed.netloc.lower().startswith("search.") or \
                        parsed.path.lower().endswith(".pdf"):
                    logger.info("dynamic_tools: search grounding skipping "
                                "non-content url=%s", url)
                    continue
                try:
                    text = self._fetch_url_text(url)
                except Exception as exc:
                    logger.info("dynamic_tools: search grounding fetch failed url=%s err=%s", url, exc)
                    continue
                if text:
                    texts.append(text)
                    sources.append(url)
                else:
                    logger.info("dynamic_tools: search grounding fetch empty url=%s", url)
            block = ""
            if texts:
                extract = self._distill_reference_texts(request, texts)
                if extract:
                    block = extract + "\n來源:\n" + "\n".join(sources)
                    logger.info("dynamic_tools: search grounding hit %d page(s) -> %d chars: %s",
                                len(texts), len(extract), _tail(extract, 200))
                else:
                    logger.info("dynamic_tools: search grounding distill returned NONE "
                                "for %d fetched page(s) %s", len(texts), sources)
            if not block:
                logger.info("dynamic_tools: search grounding found nothing usable")
            # Cache the raw texts (misses too — no re-query today); distillation
            # reruns on each hit so prompt fixes don't need a fresh search.
            state["cache"][request] = {"texts": texts, "sources": sources}
            self._save_search_state(state)
            return block or None
        except Exception:
            logger.exception("dynamic_tools: search grounding failed; continuing without it")
            return None

    def _explore_api(self, request: str, knowledge_rows: list,
                     references: str | None = None) -> str | None:
        """Phase 0: generate + run a tiny discovery script to capture actual API
        field names. Returns the captured structure text, or None if exploration
        fails / is not needed (pure-computation requests)."""
        explorer_prompt = (
            "你是 Python 工程師。請為以下需求寫一個「API 探索腳本」（不是最終工具）。\n"
            "目的：呼叫相關 API 一次，把回傳的 JSON 欄位結構印出，供後續正式腳本使用正確欄位名。\n\n"
            f"需求：{request}\n"
            + self._references_block(references)
            + "\n規則：\n"
            f'1. 若需求需要外部 API（天氣、股票、匯率等）：呼叫 API，然後：\n'
            f'   print("{_API_STRUCT_START}")\n'
            '   print(json.dumps(response, indent=2, ensure_ascii=False)[:1200])\n'
            f'   print("{_API_STRUCT_END}")\n'
            '2. 若需求是純計算（不需外部 API，資料已在 request 中或參考資料已給出所需數值）：'
            '只 print("NO_EXTERNAL_API")，絕不可為了重新取得參考資料已有的數值而猜測 API 端點\n'
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

    @staticmethod
    def _references_block(references: str | None) -> str:
        if not references:
            return ""
        return (
            "\n<參考資料（公式、名詞定義與慣例以此為準）>\n"
            + references
            + "\n</參考資料>\n"
            "（參考資料若已給出需求所需的數值（利率、匯率、現值等），"
            "就把該數值放進 DEFAULTS 直接使用，並在計算依據印出其來源與資訊時點；"
            "絕不可為了重新取得這個值而呼叫或猜測任何 API 端點。）\n"
        )

    def _generate_code(self, request: str, knowledge_rows: list, *, think: bool = False,
                       api_structure: str | None = None,
                       references: str | None = None) -> str:
        prompt = self._build_codegen_prompt(request, knowledge_rows,
                                            api_structure=api_structure,
                                            references=references)
        response = self.client.generate(prompt, temperature=0.0, think=think)
        self._last_meta = _extract_meta(response)
        return _extract_code(response)

    def _repair_code(self, request: str, code: str, error: str, knowledge_rows: list, *,
                     think: bool = False, api_structure: str | None = None,
                     references: str | None = None) -> str:
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
            + self._references_block(references)
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
                              api_structure: str | None = None,
                              references: str | None = None) -> str:
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
            + self._references_block(references)
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
        """Only the STRUCTURAL CONTRACT lives here — things the surrounding code
        parses or enforces (markers, # requires, params.json, sandbox limits).
        All coding techniques/disciplines are RAG rules (knowledge_db CODEGEN_SEED
        always-on entries + distilled rules) injected via the methodology block,
        so they evolve through distillation instead of being hardcoded."""
        from .knowledge_db import format_codegen_knowledge_block

        methodology = format_codegen_knowledge_block(knowledge_rows) if knowledge_rows else "(無)"
        approved = ", ".join(sorted(_APPROVED_PACKAGES))
        return (
            "可用環境：Python 3 標準函式庫；需要第三方套件時，在檔案最上方用註解列出，"
            "例如：# requires: beautifulsoup4（會自動 pip install 到專屬 venv）。"
            f"第三方套件僅限核准清單：{approved}——清單外的（如 scipy）一律禁止，"
            "需要清單外才有的演算法（統計、線性代數等）就用 numpy 或自己用基本運算實作。"
            "絕不可臆造標準函式庫不存在的函式（不確定某函式是否存在，就自己實作該計算）。"
            "優先使用標準函式庫 urllib + 公開 JSON API 以減少相依與加快執行；"
            "方法論已給出可照抄端點/範本時，必須照抄範本，不可自行換用其他套件或端點。\n\n"
            "<代碼開發方法論>\n" + methodology + "\n</代碼開發方法論>\n"
            "（方法論是依本需求挑選的既驗證經驗，務必優先遵循；與需求無關的條目忽略。）\n\n"
            "結構契約（外層程式會解析這些輸出，必須完全遵守）：\n"
            "1. 可替換的輸入值（金額、城市、股票代碼、日期等）收進腳本頂端的 DEFAULTS dict，"
            "並讀同目錄 params.json 覆寫：params = dict(DEFAULTS)，"
            "若 os.path.exists('params.json') 則 params.update(json.load(open(...)))；"
            "之後一律用 params[...]。META 的 param_schema 每個 name 必須與 DEFAULTS 的 key 完全一致。\n"
            f"2. 成功時最終答案印在標記之間：先 print(\"{ANSWER_START}\")，"
            f"接著 print 人類可讀答案，最後 print(\"{ANSWER_END}\")。"
            "數值計算類答案必須同時印出計算依據，形式依題型："
            "時間序列計算印資料源、期間起迄日期、期初/期末原始值，"
            "若答案取決於序列中的關鍵資料點（如極值），也要印出該關鍵點的日期與數值，"
            "且必須是『真正決定答案的那組』資料點，與答案數值自洽"
            "（例如最大回撤要在掃描時記下達成最大跌幅當下的峰值與谷值日期/價格一起印出，"
            "印出的峰→谷跌幅必須等於答案的回撤率；不可印掃描結束時的最後峰值），"
            "且日期與數值必須取自 API 實際回傳的資料點（例如把所用資料點的 timestamp 轉成日期印出），"
            "不可印程式自行假設的期間；"
            "以單一外部參數計算（利率、匯率等）則印參數值、參數來源（URL/機構）與資訊時點"
            "——驗證層會檢查依據與需求是否一致，缺依據或依據造假視為失敗。\n"
            "3. 失敗時讓例外直接拋出、以非零碼結束——stderr 的 traceback 會觸發外層自動修復；"
            f"絕不要把錯誤訊息印進 {ANSWER_START} 區塊。\n"
            "4. 沙箱限制：不可讀取任何祕密環境變數（OPENCLAW_*、API token 等）；"
            "不可刪除檔案、不可開 shell/subprocess。讀 params.json 是允許且必要的。\n"
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

    def _validate_answer(self, request: str, answer: str,
                         references: str | None = None) -> tuple[bool, str]:
        """LLM gate on a successful run's output: does it plausibly answer the
        request? Lenient by design — only clear mismatches FAIL. Any validator
        error fails open (True) so a sick validator can never brick /new."""
        refs_block = ""
        if references:
            refs_block = (
                "已查證的參考資料（答案使用的外部參數值——利率、費率、現值等——"
                "必須與此一致；參考資料給了值而答案用了別的值就 FAIL；"
                "參考資料標明某值僅是『檢討中/提案中/尚未生效』，"
                "答案卻把它當成現行值使用，也是 FAIL。"
                "判定外部參數值時一律以這份參考資料為準——"
                "你記憶中的舊值可能已過期，不可拿來推翻參考資料）：\n"
                + _tail(references, 600) + "\n"
            )
        prompt = (
            "判斷下面的『答案』是否合理回應了『需求』。寬鬆判定：主題正確、有實質內容就算 PASS；"
            "答案簡短但正確也算 PASS。只有以下情況才 FAIL：\n"
            "- 答非所問：回答的『事情』不是需求問的那件事。即使提到相同的城市/股票/日期也一樣"
            "（例如問機票價格卻回天氣、問報酬率卻回氣溫）。\n"
            "- 夾帶需求沒問的多餘資料（例如問股票報酬卻多印一行天氣）。\n"
            "- 內容空洞、只有佔位文字、或自述『無法取得資料』而沒有給出實際答案。\n"
            "- 夾帶錯誤訊息/traceback、或明顯編造的假值。\n"
            "- 答案文字夾帶未替換的程式佔位符（如 {params['x']}、{變數} 之類的樣板殘骸"
            "出現在人類可讀文字裡）。\n"
            "- 數值是可疑的退化值：報酬率/變化率恰為 0.00%、最高=最低、"
            "所有數值完全相同——真實資料幾乎不會這樣，多半是程式取值錯誤。\n"
            "- 數值計算類答案沒有附計算依據——沒有依據就無法驗證，視為不合格。"
            "依據形式依題型：時間序列計算要有期間起迄日期與期初/期末原始值，"
            "且若答案取決於序列中的關鍵資料點（如最大回撤的峰值/谷值），"
            "必須印出該關鍵點的日期與數值，缺了就 FAIL；"
            "以單一外部參數計算（利率、匯率等）要有參數值、參數來源與資訊時點。\n"
            "- 計算依據與需求不一致：用今天日期換算需求的期間語意（如『今年以來』『最近一週』），"
            "若依據顯示的期間起迄落在需求期間之外、或標的不符，FAIL。"
            "例外：期初日期是期間起點前最近一個資料點（差距幾天內，常見基期慣例）算一致；"
            "偏離期間起點數週以上就是取錯基準，FAIL。\n"
            "- 依據與答案數值不自洽：用依據裡的關鍵資料點驗算答案"
            "（例如最大回撤＝(峰值−谷值)/峰值，所述峰/谷算出的跌幅必須等於答案的回撤率），"
            "明顯對不上就是依據造假或程式取錯資料點，FAIL。\n"
            + refs_block
            + f"今天日期：{date.today().isoformat()}\n"
            f"需求：{request}\n"
            f"答案：\n{_tail(answer, 800)}\n"
            "只輸出一行：PASS 或 FAIL: <一句原因>。不要任何解說。"
        )
        saved_model = self.client.model
        try:
            # The gate is the last line of defense for answer quality; the fast
            # model rubber-stamped period/scope mismatches the strong one catches.
            self.client.model = self.strong_model
            raw = self.client.generate(prompt, temperature=0.0, think=False).strip()
        except Exception:
            logger.exception("dynamic_tools: answer validation call failed; failing open")
            return True, ""
        finally:
            self.client.model = saved_model
        first = raw.splitlines()[0].strip() if raw else ""
        if first.upper().startswith("FAIL"):
            normalized = first.replace("：", ":", 1)
            reason = normalized.split(":", 1)[1].strip() if ":" in normalized else ""
            return False, reason
        return True, ""

    def _preflight(self, request: str, topical: list) -> tuple[bool, str, list | None]:
        """One strong-model call doing both pre-codegen judgments (they used to
        be two serial calls — minutes of extra latency on a local model):

        1. Feasibility gate: can the needed data come from a key-free,
           programmatically accessible public source (or pure computation)?
           INFEASIBLE → honest refusal upstream, zero generations burned, and
           the cascade never gets coaxed into substituting unrelated data.
        2. Topical rule selection (open-world, no keyword lists): which stored
           recipes/methods apply to this request.

        Returns (feasible, why, selected_topical). selected_topical None means
        the selection part is unusable → caller falls back to keyword scoring.
        Any call error fails open: (True, "", None)."""
        listing = "\n".join(
            f"{i}. [{row.category}] {row.title}" for i, row in enumerate(topical, start=1)
        ) or "(無)"
        prompt = (
            "對下面的需求做兩個判斷，各輸出一行：\n"
            "第1行（可行性判斷）：要用一支 Python 腳本自動完成需求，判斷『需求所需的資料』"
            "能否從免金鑰、可程式化存取的公開來源取得（或純計算、不需外部資料）。"
            "寬鬆判定：純數學計算、天氣、股價/匯率/指數、維基百科等公開資訊都算 FEASIBLE；"
            "只有資料明確需要付費/授權 API、需要登入帳號、或根本沒有公開程式化來源時"
            "才是 INFEASIBLE（例如：即時機票票價、演唱會剩餘票數、私人帳戶資料）。"
            "輸出 FEASIBLE 或 INFEASIBLE: <一句原因>。\n"
            "第2行（挑出與需求相關的規則）：從規則清單挑出『這個需求真的會用到』的，"
            "例如股價需求挑金融資料源規則、天氣需求挑天氣資料源規則；"
            "不相關的絕對不要挑（注入無關範例會汙染生成的程式）。"
            "輸出相關規則的編號（逗號分隔），沒有相關的就輸出 NONE。\n"
            f"需求：{request}\n"
            f"規則清單：\n{listing}\n"
            "只輸出兩行，不要任何解說。"
        )
        saved_model = self.client.model
        try:
            self.client.model = self.strong_model
            raw = self.client.generate(prompt, temperature=0.0, think=False).strip()
        except Exception:
            logger.exception("dynamic_tools: preflight failed; failing open")
            return True, "", None
        finally:
            self.client.model = saved_model
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        first = lines[0] if lines else ""
        if first.upper().startswith("INFEASIBLE"):
            normalized = first.replace("：", ":", 1)
            reason = normalized.split(":", 1)[1].strip() if ":" in normalized else ""
            return False, reason, None
        if not topical:
            return True, "", []
        rest = " ".join(lines[1:])
        if "NONE" in rest.upper():
            return True, "", []
        indices = [int(m) for m in re.findall(r"\d+", rest)]
        picked = [topical[i - 1] for i in indices if 1 <= i <= len(topical)]
        if not picked:
            return True, "", None
        return True, "", picked[:4]

    # ── distillation ────────────────────────────────────────────────────────

    def _load_rules_split(self) -> tuple[list, list]:
        """(always_on, topical) rules from the knowledge DB. Always-on rules
        ("*" keyword) are injected into every codegen prompt; topical rules are
        picked per-request by the LLM in _preflight."""
        if self.knowledge_db is None:
            return [], []
        try:
            rows = self.knowledge_db.all_codegen_knowledge()
        except Exception:
            logger.exception("dynamic_tools: all_codegen_knowledge failed")
            return [], []
        always_on = [r for r in rows if "*" in r.keywords]
        topical = [r for r in rows if "*" not in r.keywords]
        return always_on, topical

    def _keyword_fallback_rules(self, request: str, always_on: list) -> list:
        try:
            return self.knowledge_db.retrieve_codegen_knowledge(request, k=6)
        except Exception:
            logger.exception("dynamic_tools: retrieve_codegen_knowledge failed")
            return always_on

    def _merge_keyword_topicals(self, request: str, selected: list) -> list:
        """Deterministic floor under the LLM rule selector. The preflight
        listing is re-ordered every run (confidence/updated_at churn from
        distillation), so which rules the LLM picks is unstable — a critical
        recipe it picked last run can silently drop out the next. A topical
        rule whose author-declared keywords literally appear in the request is
        related by definition, so merge those in regardless of the LLM's pick.
        Only runs when the LLM picked a non-empty set: an explicit NONE verdict
        stays authoritative."""
        if self.knowledge_db is None:
            return selected
        try:
            ranked = self.knowledge_db.retrieve_codegen_knowledge(request, k=6)
        except Exception:
            logger.exception("dynamic_tools: keyword-floor retrieval failed")
            return selected
        have = {r.knowledge_id for r in selected}
        extras = [r for r in ranked
                  if "*" not in r.keywords and r.knowledge_id not in have]
        if extras:
            logger.info("dynamic_tools: keyword floor added rules=%s",
                        [r.title for r in extras])
        return (selected + extras)[:6]

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
        # Distillation runs synchronously in the answer path (only after a run
        # that needed ≥2 generations); use the fast model so it adds seconds,
        # not minutes — abstraction quality matters less than latency here.
        saved_model = self.client.model
        try:
            self.client.model = self.fast_model
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
        finally:
            self.client.model = saved_model

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

# Import name → pip distribution name for the well-known mismatches; installing
# the raw module name makes an *approved* package look unapproved.
_MODULE_TO_PIP: dict[str, str] = {
    "dateutil": "python-dateutil",
    "bs4": "beautifulsoup4",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "sklearn": "scikit-learn",
    "cv2": "opencv-python",
}


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


def _sandbox_wrapper_failed(stderr: str) -> bool:
    low = (stderr or "").lower()
    return "sandbox-exec" in low and (
        "sandbox_apply" in low
        or "operation not permitted" in low
        or "profile" in low
    )


def _first_line(text: str, limit: int) -> str:
    line = (text or "").strip().splitlines()[0] if text.strip() else ""
    return line[:limit]


def _opencode_cli_model(model: str) -> str:
    model = (model or "big-pickle").strip()
    return model if "/" in model else f"opencode/{model}"


def build_dynamic_tool_runner_from_settings(settings) -> DynamicToolRunner | None:
    """Build a runner from AssistantSettings.

    Default behavior stays local Ollama. ``OPENCLAW_CODEGEN_BACKEND=opencode``
    opts /new into OpenCode Big Pickle for text generation while preserving the
    existing generated-tool sandbox, manifest reuse, validation, and repairs.
    """
    codegen_backend = (getattr(settings, "openclaw_codegen_backend", None) or "").strip().lower()
    if codegen_backend == "opencode":
        model = (getattr(settings, "openclaw_opencode_model", "big-pickle") or "big-pickle").strip()
        timeout = max(60, int(getattr(settings, "openclaw_opencode_timeout_seconds", 900)))
        cli_model = _opencode_cli_model(model)
        if probe_opencode_cli(model=cli_model, timeout=min(timeout, 30)):
            client = OpenCodeCliTextClient(model=cli_model, timeout_seconds=timeout)
            logger.info("dynamic_tools: using OpenCode CLI codegen backend model=%s", cli_model)
            return _build_runner_with_client(
                settings, client, fast_model=cli_model, strong_model=cli_model)
        logger.warning("dynamic_tools: OpenCode CLI unavailable; falling back to Ollama if configured")
    elif codegen_backend and codegen_backend != "ollama":
        logger.warning("dynamic_tools: unsupported codegen backend=%s; falling back to Ollama", codegen_backend)

    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    if backend != "ollama":
        if backend:
            logger.warning("dynamic_tools: unsupported local text backend=%s", backend)
        return None
    strong_model = _select_model(settings.openclaw_local_text_model)
    if not strong_model:
        return None
    # Fast tier-1 model (code-specialized). Falls back to the strong model when
    # unset, collapsing the cascade to single-tier (old) behavior.
    fast_model = (getattr(settings, "openclaw_codegen_fast_model", None) or "").strip() or strong_model

    # Codegen needs more time + context than the NL router.
    # num_ctx=8192: prevents 4096-default from leaving too few tokens for response.
    # num_predict=2000: generation time scales with tokens actually produced, so a
    #   generous cap costs nothing on normal scripts (~600-800 tok) and avoids the
    #   truncate→regenerate cycle that a 1000 cap kept triggering on verbose tiers.
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
        num_predict=2000,
    )
    return _build_runner_with_client(settings, client, fast_model=fast_model, strong_model=strong_model)


def _build_runner_with_client(
    settings,
    client: TextGenerationClient,
    *,
    fast_model: str,
    strong_model: str,
) -> DynamicToolRunner:
    """Shared runner wiring after selecting the text generation backend."""

    knowledge_db = None
    try:
        from .knowledge_db import KnowledgeDatabase

        knowledge_db = KnowledgeDatabase(settings.knowledge_db_path)
        knowledge_db.seed_codegen_knowledge()
    except Exception:
        logger.exception("dynamic_tools: knowledge DB init failed; continuing without RAG")

    tools_dir = _resolve_tools_dir()
    runner = DynamicToolRunner(
        client=client, tools_dir=tools_dir, knowledge_db=knowledge_db,
        fast_model=fast_model, strong_model=strong_model,
        # Self-learning: abstract hard-won repairs into transferable RAG rules so
        # novel question types get easier over time instead of relying on seeds.
        distill_enabled=True,
    )
    try:
        from .web_search import web_search

        runner.search_fn = lambda q, max_results: web_search(
            q, max_results=max_results)
    except Exception:
        logger.exception("dynamic_tools: search grounding backend unavailable")
    return runner


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
