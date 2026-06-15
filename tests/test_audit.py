"""Tests for the audit subsystem — scanner, rules, report, CLI."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from agent_governance.audit import (
    ALL_RULES,
    Finding,
    Inventory,
    format_json,
    format_markdown,
    run_rules,
    scan,
)
from agent_governance.audit.cli import main as cli_main
from agent_governance.audit.rules import (
    HardcodedSecretRule,
    LLMCallNoEvalRule,
    LLMCallNoTraceRule,
    MissingEnvExampleRule,
    MissingGovernanceNodeRule,
    MissingProvenanceDisclaimerRule,
    MissingTraceIdInResponseRule,
    MissingUrlOutputValidationRule,
    PromptLacksRefusalLanguageRule,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Build a fixture repo. `files` maps repo-relative path → content."""
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dedent(content), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def test_scanner_detects_anthropic_llm_call(tmp_path):
    root = _repo(tmp_path, {
        "app.py": """\
            from anthropic import Anthropic
            client = Anthropic()
            r = client.messages.create(model="x", messages=[])
        """,
    })
    inv = scan(root)
    assert len(inv.llm_calls) == 1
    assert inv.llm_calls[0].sdk == "anthropic"


def test_scanner_detects_langchain_chat_anthropic_call(tmp_path):
    root = _repo(tmp_path, {
        "app.py": """\
            from langchain_anthropic import ChatAnthropic
            llm = ChatAnthropic(model="x")
            r = llm.ainvoke([])
        """,
    })
    inv = scan(root)
    assert len(inv.llm_calls) == 1
    assert inv.llm_calls[0].sdk == "langchain_anthropic"


def test_scanner_detects_named_prompt(tmp_path):
    root = _repo(tmp_path, {
        "prompts.py": '''\
            SUMMARIZE_SYSTEM = """You are an analyst. Never invent URLs. Do not speculate."""
        ''',
    })
    inv = scan(root)
    assert len(inv.prompts) == 1
    assert inv.prompts[0].name == "SUMMARIZE_SYSTEM"
    assert inv.prompts[0].has_refusal_language is True


def test_scanner_detects_long_unnamed_string_as_prompt(tmp_path):
    """A 200+ char string literal still counts even if the name doesn't match."""
    long_text = "Answer questions accurately " * 10
    root = _repo(tmp_path, {
        "x.py": f'foo = """{long_text}"""\n',
    })
    inv = scan(root)
    assert len(inv.prompts) == 1
    assert inv.prompts[0].has_refusal_language is False  # no "do not" / "never"


def test_scanner_detects_provenance_language_in_prompt(tmp_path):
    root = _repo(tmp_path, {
        "p.py": '''\
            SYNTH_SYSTEM = """Owner data shown here is synthetic and illustrative only.
            Add a disclaimer about non-authoritative data when you summarize."""
        ''',
    })
    inv = scan(root)
    assert inv.prompts[0].has_provenance_language is True


def test_scanner_detects_trace_providers(tmp_path):
    root = _repo(tmp_path, {
        "obs.py": "from langfuse import Langfuse\nimport langsmith\n",
    })
    inv = scan(root)
    assert "langfuse" in inv.trace_providers
    assert "langsmith" in inv.trace_providers


def test_scanner_detects_eval_dir_and_golden_file(tmp_path):
    _repo(tmp_path, {
        "evals/golden.yaml": "cases:\n  - foo\n",
        "scripts/eval.py": "from evalkit import judge\n",
    })
    inv = scan(tmp_path)
    assert "evals" in inv.eval_paths


def test_scanner_detects_langgraph_usage(tmp_path):
    root = _repo(tmp_path, {
        "g.py": "from langgraph.graph import StateGraph\n",
    })
    inv = scan(root)
    assert inv.uses_langgraph is True


def test_scanner_detects_governance_node_wired(tmp_path):
    root = _repo(tmp_path, {
        "g.py": "from agent_governance import build_governance_node, URLAllowlistCheck\n",
    })
    inv = scan(root)
    assert inv.governance_node_wired is True
    assert inv.has_url_allowlist_check is True


def test_scanner_detects_env_example(tmp_path):
    _repo(tmp_path, {".env.example": "FOO=\n"})
    assert scan(tmp_path).env_example_present is True


def test_scanner_detects_url_response_field(tmp_path):
    root = _repo(tmp_path, {
        "models.py": """\
            from pydantic import BaseModel
            class R(BaseModel):
                url: str
                trace_id: str | None = None
        """,
    })
    inv = scan(root)
    assert inv.url_response_fields and inv.url_response_fields[0][0] == "models.py"
    assert inv.trace_id_in_response is True


def test_scanner_skips_venv_and_node_modules(tmp_path):
    root = _repo(tmp_path, {
        "app.py": "import os\n",
        ".venv/lib/site-packages/some_pkg/x.py": "from anthropic import Anthropic\n",
        "node_modules/foo/y.py": "from openai import OpenAI\n",
    })
    inv = scan(root)
    assert inv.llm_calls == []


def test_scanner_detects_anthropic_key_pattern(tmp_path):
    root = _repo(tmp_path, {
        "leak.py": 'API_KEY = "sk-ant-' + 'a' * 50 + '"\n',
    })
    inv = scan(root)
    assert inv.hardcoded_secret_hits
    assert inv.hardcoded_secret_hits[0][2] == "anthropic_api_key"


def test_scanner_handles_syntax_error_gracefully(tmp_path):
    root = _repo(tmp_path, {
        "good.py": "x = 1\n",
        "broken.py": "def foo(:\n",
    })
    inv = scan(root)  # must not raise
    assert inv.file_count == 2


# ---------------------------------------------------------------------------
# Rules — each one in isolation against a crafted Inventory.
# ---------------------------------------------------------------------------


def _inv(**kwargs) -> Inventory:
    """Build a minimal Inventory with overrides."""
    inv = Inventory(root=Path("."))
    for k, v in kwargs.items():
        setattr(inv, k, v)
    return inv


def test_rule_llm_call_no_eval_fires():
    from agent_governance.audit.scanner import LLMCallSite
    inv = _inv(llm_calls=[LLMCallSite("a.py", 1, "anthropic", "Anthropic.messages.create")])
    findings = LLMCallNoEvalRule().check(inv)
    assert findings and findings[0].rule_id == "LLM_CALL_NO_EVAL"


def test_rule_llm_call_no_eval_silent_when_evals_present():
    from agent_governance.audit.scanner import LLMCallSite
    inv = _inv(
        llm_calls=[LLMCallSite("a.py", 1, "anthropic", "x")],
        eval_paths=["evals"],
    )
    assert LLMCallNoEvalRule().check(inv) == []


def test_rule_llm_call_no_trace_fires():
    from agent_governance.audit.scanner import LLMCallSite
    inv = _inv(llm_calls=[LLMCallSite("a.py", 1, "anthropic", "x")])
    assert LLMCallNoTraceRule().check(inv)


def test_rule_llm_call_no_trace_silent_with_langfuse():
    from agent_governance.audit.scanner import LLMCallSite
    inv = _inv(
        llm_calls=[LLMCallSite("a.py", 1, "anthropic", "x")],
        trace_providers={"langfuse"},
    )
    assert LLMCallNoTraceRule().check(inv) == []


def test_rule_missing_env_example():
    inv = _inv(env_example_present=False)
    assert MissingEnvExampleRule().check(inv)
    inv2 = _inv(env_example_present=True)
    assert MissingEnvExampleRule().check(inv2) == []


def test_rule_hardcoded_secret_fires_per_hit():
    inv = _inv(hardcoded_secret_hits=[("a.py", 5, "anthropic_api_key"), ("b.py", 9, "openai_api_key")])
    findings = HardcodedSecretRule().check(inv)
    assert len(findings) == 2
    assert all(f.severity == "violation" for f in findings)


def test_rule_missing_governance_node_only_when_llm_calls_present():
    from agent_governance.audit.scanner import LLMCallSite
    # No LLM calls → no finding
    assert MissingGovernanceNodeRule().check(_inv()) == []
    # LLM calls but no governance → fires
    inv = _inv(llm_calls=[LLMCallSite("a.py", 1, "anthropic", "x")])
    assert MissingGovernanceNodeRule().check(inv)
    # LLM calls + governance wired → silent
    inv2 = _inv(
        llm_calls=[LLMCallSite("a.py", 1, "anthropic", "x")],
        governance_node_wired=True,
    )
    assert MissingGovernanceNodeRule().check(inv2) == []


def test_rule_missing_provenance_disclaimer():
    from agent_governance.audit.scanner import PromptSite
    p_synthetic = PromptSite("p.py", 1, "S", 400, False, True)
    p_clean = PromptSite("c.py", 1, "C", 400, False, False)
    # Synthetic prompt + no governance → fires
    inv = _inv(prompts=[p_synthetic])
    assert MissingProvenanceDisclaimerRule().check(inv)
    # Same but governance wired → silent
    inv2 = _inv(prompts=[p_synthetic], governance_node_wired=True)
    assert MissingProvenanceDisclaimerRule().check(inv2) == []
    # No synthetic-flavored prompts → silent
    inv3 = _inv(prompts=[p_clean])
    assert MissingProvenanceDisclaimerRule().check(inv3) == []


def test_rule_missing_url_output_validation():
    # url field present, no allowlist → fires
    inv = _inv(url_response_fields=[("m.py", 7)])
    assert MissingUrlOutputValidationRule().check(inv)
    # allowlist wired → silent
    inv2 = _inv(url_response_fields=[("m.py", 7)], has_url_allowlist_check=True)
    assert MissingUrlOutputValidationRule().check(inv2) == []


def test_rule_prompt_lacks_refusal_language():
    from agent_governance.audit.scanner import PromptSite
    # short prompt → not flagged even without refusal language
    short = PromptSite("p.py", 1, "X", 100, False, False)
    assert PromptLacksRefusalLanguageRule().check(_inv(prompts=[short])) == []
    # long prompt without refusal → flagged
    long = PromptSite("p.py", 1, "X", 500, False, False)
    assert PromptLacksRefusalLanguageRule().check(_inv(prompts=[long]))
    # long prompt with refusal → silent
    long_ok = PromptSite("p.py", 1, "X", 500, True, False)
    assert PromptLacksRefusalLanguageRule().check(_inv(prompts=[long_ok])) == []


def test_rule_missing_trace_id_in_response():
    # tracing wired + response model has no trace_id → fires
    inv = _inv(
        trace_providers={"langfuse"},
        api_response_models=[("s.py", 1)],
        trace_id_in_response=False,
    )
    assert MissingTraceIdInResponseRule().check(inv)
    # trace_id field present → silent
    inv2 = _inv(
        trace_providers={"langfuse"},
        api_response_models=[("s.py", 1)],
        trace_id_in_response=True,
    )
    assert MissingTraceIdInResponseRule().check(inv2) == []
    # no tracing wired → silent (no obligation to surface what doesn't exist)
    inv3 = _inv(api_response_models=[("s.py", 1)], trace_id_in_response=False)
    assert MissingTraceIdInResponseRule().check(inv3) == []


# ---------------------------------------------------------------------------
# End-to-end: scan + run_rules on a crafted "bad" repo
# ---------------------------------------------------------------------------


def test_end_to_end_bad_repo_fires_multiple_rules(tmp_path):
    """A deliberately bad fixture repo should trigger several rules at once."""
    root = _repo(tmp_path, {
        "app.py": '''\
            from anthropic import Anthropic
            client = Anthropic()
            SYNTH_PROMPT = """Owner data here is synthetic. Summarize it for the user."""
            def main():
                return client.messages.create(model="x", messages=[])
        ''',
        "schemas.py": """\
            from pydantic import BaseModel
            class R(BaseModel):
                url: str
        """,
    })
    inv = scan(root)
    findings = run_rules(inv)
    rule_ids = {f.rule_id for f in findings}
    # All of these should fire on this repo:
    assert "LLM_CALL_NO_EVAL" in rule_ids
    assert "LLM_CALL_NO_TRACE" in rule_ids
    assert "MISSING_ENV_EXAMPLE" in rule_ids
    assert "MISSING_GOVERNANCE_NODE" in rule_ids
    assert "MISSING_PROVENANCE_DISCLAIMER" in rule_ids
    assert "MISSING_URL_OUTPUT_VALIDATION" in rule_ids


def test_end_to_end_well_governed_repo_yields_few_findings(tmp_path):
    """A repo with eval + trace + governance wired should produce minimal findings."""
    root = _repo(tmp_path, {
        ".env.example": "ANTHROPIC_API_KEY=\n",
        "evals/golden.yaml": "cases: []\n",
        "app.py": """\
            import langfuse
            from anthropic import Anthropic
            from agent_governance import build_governance_node, URLAllowlistCheck
            client = Anthropic()
            def go(): return client.messages.create(model="x", messages=[])
        """,
    })
    inv = scan(root)
    findings = run_rules(inv)
    # The high-severity rules should be silent.
    sev = {f.severity for f in findings}
    assert "violation" not in sev


# ---------------------------------------------------------------------------
# Report formatters
# ---------------------------------------------------------------------------


def test_markdown_report_no_findings(tmp_path):
    inv = scan(_repo(tmp_path, {"x.py": "x = 1\n"}))
    md = format_markdown(inv, [])
    assert "No findings" in md
    assert "## Inventory" in md


def test_markdown_report_groups_by_severity(tmp_path):
    inv = scan(_repo(tmp_path, {}))
    fs = [
        Finding("A", "info", "info-thing", "d", "r", file="x", line=1),
        Finding("B", "violation", "bad-thing", "d", "r", file="x", line=1),
        Finding("C", "warning", "warn-thing", "d", "r", file="x", line=1),
    ]
    md = format_markdown(inv, fs)
    # Violation must come before warning, which must come before info.
    pos_v = md.find("bad-thing")
    pos_w = md.find("warn-thing")
    pos_i = md.find("info-thing")
    assert 0 < pos_v < pos_w < pos_i


def test_json_report_is_valid_and_includes_findings(tmp_path):
    inv = scan(_repo(tmp_path, {"x.py": "x = 1\n"}))
    fs = [Finding("R", "warning", "T", "D", "Rec", "x.py", 7)]
    payload = json.loads(format_json(inv, fs))
    assert payload["tool"] == "agent-governance audit"
    assert payload["findings"][0]["rule_id"] == "R"
    assert payload["findings"][0]["location"] == "x.py:7"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_audit_runs_and_returns_zero_on_clean_repo(tmp_path, capsys):
    _repo(tmp_path, {
        ".env.example": "X=\n",
        "evals/golden.yaml": "cases: []\n",
        "app.py": """\
            import langfuse
            from anthropic import Anthropic
            from agent_governance import build_governance_node
            c = Anthropic()
            def go(): return c.messages.create(model="x", messages=[])
        """,
    })
    code = cli_main(["audit", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Inventory" in out


def test_cli_fail_on_warning_returns_nonzero_when_warning_present(tmp_path):
    _repo(tmp_path, {
        "app.py": """\
            from anthropic import Anthropic
            c = Anthropic()
            def go(): return c.messages.create(model="x", messages=[])
        """,
    })
    code = cli_main(["audit", str(tmp_path), "--fail-on", "warning"])
    assert code == 1


def test_cli_json_output_parses(tmp_path, capsys):
    _repo(tmp_path, {"a.py": "x = 1\n"})
    code = cli_main(["audit", str(tmp_path), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["tool"] == "agent-governance audit"


def test_cli_writes_to_output_file(tmp_path):
    _repo(tmp_path, {"a.py": "x = 1\n"})
    out_path = tmp_path / "report.md"
    code = cli_main(["audit", str(tmp_path), "-o", str(out_path)])
    assert code == 0
    assert out_path.read_text(encoding="utf-8").startswith("# agent-governance audit")


def test_cli_rejects_invalid_path(tmp_path, capsys):
    code = cli_main(["audit", str(tmp_path / "does-not-exist")])
    assert code == 2


def test_cli_github_requires_token(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, {"a.py": "x = 1\n"})
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    code = cli_main(["audit", str(tmp_path), "--github", "owner/repo"])
    assert code == 2
    assert "requires --github-token" in capsys.readouterr().err
