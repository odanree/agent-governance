"""Repo auditor — static analysis for LLM agent governance gaps.

Walks a Python repo, builds an inventory of LLM call sites, prompts, evals,
tracing, and config, then runs a registry of rules to find missing governance
(no evals, no tracing, prompt with no refusal language, etc.) and recommends
fixes — typically by pointing to a runtime check from this package.

Entry points:
    agent-governance audit ./path/to/repo
    from agent_governance.audit import scan, run_rules, ALL_RULES
"""

from agent_governance.audit.report import format_json, format_markdown
from agent_governance.audit.rules import (
    ALL_RULES,
    Finding,
    Rule,
    run_rules,
)
from agent_governance.audit.scanner import (
    Inventory,
    LLMCallSite,
    PromptSite,
    scan,
)

__all__ = [
    "ALL_RULES",
    "Finding",
    "Inventory",
    "LLMCallSite",
    "PromptSite",
    "Rule",
    "format_json",
    "format_markdown",
    "run_rules",
    "scan",
]
