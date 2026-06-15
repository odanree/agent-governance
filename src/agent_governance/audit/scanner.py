"""Repo scanner — walks a Python project and builds an inventory of the
governance-relevant signals the rules later evaluate.

Conservative by design: false positives are worse than false negatives in an
audit context (a wrong finding wastes a reviewer's time; a missed signal
just means a rule misses one repo). Pattern matches err on the side of
explicit imports and named API surfaces rather than runtime introspection.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMCallSite:
    file: str   # repo-relative path
    line: int
    sdk: str    # "anthropic" | "openai" | "langchain_anthropic" | "langchain_openai" | "langchain_core" | "litellm"
    call: str   # the dotted callable expression, e.g. "ChatAnthropic.ainvoke"


@dataclass(frozen=True)
class PromptSite:
    file: str
    line: int
    name: str           # variable name or "<inline>"
    length: int         # char count of the literal
    has_refusal_language: bool  # do not / never / refuse / forbidden / etc.
    has_provenance_language: bool  # synthetic / illustrative / disclaimer / etc.


@dataclass
class Inventory:
    """Everything the rules will reason over."""

    root: Path
    llm_calls: list[LLMCallSite] = field(default_factory=list)
    prompts: list[PromptSite] = field(default_factory=list)
    eval_paths: list[str] = field(default_factory=list)   # repo-relative
    trace_providers: set[str] = field(default_factory=set)  # {"langfuse", "langsmith", ...}
    governance_node_wired: bool = False  # imports agent_governance.build_governance_node
    uses_langgraph: bool = False
    env_example_present: bool = False
    pyproject_deps: list[str] = field(default_factory=list)
    hardcoded_secret_hits: list[tuple[str, int, str]] = field(default_factory=list)  # (file, line, kind)
    url_response_fields: list[tuple[str, int]] = field(default_factory=list)         # response models with `url` field
    has_url_allowlist_check: bool = False  # imports URLAllowlistCheck or grep-finds an allowlist pattern
    api_response_models: list[tuple[str, int]] = field(default_factory=list)  # files where pydantic.BaseModel or QueryResponse-like classes live
    trace_id_in_response: bool = False  # naive: any response model defines a trace_id field

    @property
    def file_count(self) -> int:
        return self._py_file_count

    _py_file_count: int = 0


# ---------------------------------------------------------------------------
# Constants — easy to extend
# ---------------------------------------------------------------------------


_LLM_SDK_IMPORTS = {
    "anthropic": "anthropic",
    "openai": "openai",
    "langchain_anthropic": "langchain_anthropic",
    "langchain_openai": "langchain_openai",
    "langchain_core": "langchain_core",
    "langchain": "langchain",
    "litellm": "litellm",
}

# Function/method names we recognize as "this is an LLM call". Matched
# against the *last* attribute or function name in a Call expression.
_LLM_CALL_LEAVES = {
    "create",        # anthropic.messages.create / openai.chat.completions.create
    "invoke", "ainvoke",  # langchain Runnable
    "stream", "astream",
    "completion", "completions",
    "chat",
}

_TRACE_PROVIDERS = {
    "langfuse": "langfuse",
    "langsmith": "langsmith",
    "opentelemetry": "opentelemetry",
    "phoenix": "phoenix",
    "helicone": "helicone",
}

_EVAL_FRAMEWORKS = ("evalkit", "ragas", "promptfoo", "deepeval", "trulens", "uptrain")

# Refusal / boundary language a prompt should contain at minimum if it's
# being given to a model that can fabricate.
_REFUSAL_TOKENS = re.compile(
    r"\b(do not|don'?t|never|must not|forbidden|refuse|decline|cannot|do\s+nothing)\b",
    re.IGNORECASE,
)

# "Synthetic data lives here" language. If a prompt uses these, the rule
# expects a provenance / disclaimer mechanism.
_PROVENANCE_TOKENS = re.compile(
    r"\b(synthetic|illustrative|fake|fabricated|paywalled|not\s+authoritative|disclaimer|provenance)\b",
    re.IGNORECASE,
)

_PROMPT_NAME = re.compile(r"_(SYSTEM|PROMPT|MESSAGE|INSTRUCTIONS|RULES)\b")

_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{30,}")),
    ("openai_api_key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{30,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
]

_SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "site-packages",
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def scan(root: str | Path) -> Inventory:
    """Walk `root` and return a populated Inventory."""
    root_path = Path(root).resolve()
    inv = Inventory(root=root_path)

    inv.env_example_present = (root_path / ".env.example").is_file()
    inv.pyproject_deps = _read_pyproject_deps(root_path)

    py_count = 0
    for path in _walk_python(root_path):
        py_count += 1
        try:
            rel = str(path.relative_to(root_path)).replace("\\", "/")
        except ValueError:
            rel = str(path)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log.debug("audit scan: skipping %s (%s)", path, e)
            continue

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as e:
            log.debug("audit scan: syntax error in %s: %s", path, e)
            continue

        _AstWalker(rel, source, inv).visit(tree)

    inv._py_file_count = py_count
    inv.eval_paths = _find_eval_paths(root_path)
    return inv


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _walk_python(root: Path):
    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield path


def _read_pyproject_deps(root: Path) -> list[str]:
    """Crude extractor — pulls `dependencies = [...]` literals from pyproject.toml.
    Avoids `tomllib` to keep this dep-free in 3.10 host envs; we only need
    substring matching."""
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    # Match `"foo[bar]>=1.0"` strings inside the file.
    for m in re.finditer(r'["\']([A-Za-z0-9][\w\-\[\].@/:=<>!~]*)["\']', text):
        token = m.group(1)
        if any(c in token for c in (" ", "\n")):
            continue
        out.append(token)
    return out


def _find_eval_paths(root: Path) -> list[str]:
    candidates: list[str] = []
    for marker in ("evals", "eval"):
        d = root / marker
        if d.is_dir():
            candidates.append(marker)
    for pat in ("tests/test_eval*.py", "tests/eval*.py", "**/golden*.yaml", "**/golden*.json"):
        for p in root.glob(pat):
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            try:
                rel = str(p.relative_to(root)).replace("\\", "/")
            except ValueError:
                rel = str(p)
            if rel not in candidates:
                candidates.append(rel)
    return candidates


class _AstWalker(ast.NodeVisitor):
    """Single-file walker — populates the shared Inventory."""

    def __init__(self, rel_path: str, source: str, inv: Inventory) -> None:
        self.rel = rel_path
        self.source = source
        self.inv = inv
        self._imports: dict[str, str] = {}  # alias → root module
        self._has_pydantic_base_model = False

    # --- imports ---

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record_import(alias.name, alias.asname or alias.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        self._record_import(mod, mod.split(".")[0] if mod else "")
        for alias in node.names:
            name = alias.asname or alias.name
            self._imports[name] = mod or name
            self._detect_governance_import(mod, alias.name)
            if mod == "pydantic" and alias.name == "BaseModel":
                self._has_pydantic_base_model = True
        self._detect_trace_provider(mod)
        self._detect_eval_framework(mod)
        if mod.startswith("langgraph"):
            self.inv.uses_langgraph = True
        self.generic_visit(node)

    def _record_import(self, module: str, alias: str) -> None:
        if not module:
            return
        root = module.split(".")[0]
        self._imports[alias] = module
        self._detect_trace_provider(module)
        self._detect_eval_framework(module)
        if root == "langgraph":
            self.inv.uses_langgraph = True
        if root in _LLM_SDK_IMPORTS:
            # We don't record an LLMCallSite on import alone; we wait for
            # an actual Call node so a stray import doesn't false-flag.
            pass

    def _detect_governance_import(self, mod: str, name: str) -> None:
        if mod.startswith("agent_governance"):
            if name == "build_governance_node":
                self.inv.governance_node_wired = True
            if name == "URLAllowlistCheck":
                self.inv.has_url_allowlist_check = True

    def _detect_trace_provider(self, module: str) -> None:
        root = module.split(".")[0]
        if root in _TRACE_PROVIDERS:
            self.inv.trace_providers.add(_TRACE_PROVIDERS[root])

    def _detect_eval_framework(self, module: str) -> None:
        root = module.split(".")[0]
        if root in _EVAL_FRAMEWORKS:
            self.inv.eval_paths.append(f"<import:{root}>")

    # --- LLM call detection ---

    def visit_Call(self, node: ast.Call) -> None:
        leaf = _call_leaf_name(node.func)
        if leaf and leaf in _LLM_CALL_LEAVES:
            sdk = self._infer_sdk(node.func)
            if sdk:
                self.inv.llm_calls.append(
                    LLMCallSite(
                        file=self.rel,
                        line=node.lineno,
                        sdk=sdk,
                        call=_full_call_expr(node.func),
                    )
                )
        self.generic_visit(node)

    def _infer_sdk(self, expr: ast.expr) -> str | None:
        """Walk back through attribute chains to find the import root."""
        # Get the leftmost Name in the chain (e.g. for `client.messages.create`,
        # the leftmost is `client`).
        name = _leftmost_name(expr)
        if not name:
            return None
        root = self._imports.get(name, "").split(".")[0]
        if root in _LLM_SDK_IMPORTS:
            return _LLM_SDK_IMPORTS[root]
        # Also accept ChatAnthropic(...).invoke style — the variable is a
        # ChatAnthropic instance constructed in the same file. We can't
        # follow that easily; check if any class named ChatAnthropic /
        # ChatOpenAI is imported in this file.
        for alias, mod in self._imports.items():
            mod_root = mod.split(".")[0]
            if mod_root in _LLM_SDK_IMPORTS:
                # Heuristic: if the call's leftmost name was constructed from
                # a known LLM class import, count it. This catches `llm = ChatAnthropic(); llm.ainvoke(...)`.
                if name == alias.lower() or alias in ("ChatAnthropic", "ChatOpenAI", "Anthropic", "OpenAI"):
                    return _LLM_SDK_IMPORTS[mod_root]
        return None

    # --- Prompt detection ---

    def visit_Assign(self, node: ast.Assign) -> None:
        # Track named string constants that look like prompts.
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            name = target.id
            value = node.value
            literal = _string_literal(value)
            if literal is None:
                continue
            if not (_PROMPT_NAME.search(name) or len(literal) >= 200):
                continue
            self.inv.prompts.append(
                PromptSite(
                    file=self.rel,
                    line=node.lineno,
                    name=name,
                    length=len(literal),
                    has_refusal_language=bool(_REFUSAL_TOKENS.search(literal)),
                    has_provenance_language=bool(_PROVENANCE_TOKENS.search(literal)),
                )
            )
        self.generic_visit(node)

    # --- Pydantic response model detection ---

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        is_basemodel = any(
            (isinstance(b, ast.Name) and b.id == "BaseModel")
            or (isinstance(b, ast.Attribute) and b.attr == "BaseModel")
            for b in node.bases
        )
        if is_basemodel:
            # Look at field names to detect url / trace_id surfaces.
            for item in node.body:
                if not isinstance(item, ast.AnnAssign):
                    continue
                if not isinstance(item.target, ast.Name):
                    continue
                fname = item.target.id
                if fname == "url" or fname.endswith("_url"):
                    self.inv.url_response_fields.append((self.rel, item.lineno))
                if fname == "trace_id":
                    self.inv.trace_id_in_response = True
            self.inv.api_response_models.append((self.rel, node.lineno))
        self.generic_visit(node)

    # --- Secret hardcoding (string-level regex) ---

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and len(node.value) >= 16:
            for kind, pat in _SECRET_PATTERNS:
                if pat.search(node.value):
                    self.inv.hardcoded_secret_hits.append(
                        (self.rel, node.lineno, kind)
                    )
                    break
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _call_leaf_name(expr: ast.expr) -> str | None:
    """The terminal name of a Call's callable expression.
    For `a.b.c(...)` → "c"; for `f(...)` → "f"."""
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Name):
        return expr.id
    return None


def _leftmost_name(expr: ast.expr) -> str | None:
    """Walk `.value` chain to the leftmost Name node."""
    while isinstance(expr, ast.Attribute):
        expr = expr.value
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Call):
        return _leftmost_name(expr.func)
    return None


def _full_call_expr(expr: ast.expr) -> str:
    """Best-effort dotted source of a callable expression."""
    if isinstance(expr, ast.Attribute):
        return f"{_full_call_expr(expr.value)}.{expr.attr}"
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Call):
        return f"{_full_call_expr(expr.func)}(...)"
    return "<?>"


def _string_literal(value: ast.expr) -> str | None:
    """Extract a string literal from a Constant or a JoinedStr (f-string)
    with constant parts. Returns None for anything else."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.JoinedStr):
        parts: list[str] = []
        for v in value.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("{?}")
        return "".join(parts)
    return None
