"""Audit rules — reason over the Inventory and produce Findings.

Each rule is a `Protocol`-conforming object with a `rule_id`, `severity`,
and a `check(inventory) -> list[Finding]` method. The registry `ALL_RULES`
is the default set; users wire their own by importing `Rule`, `Finding`,
and `Severity` and calling `run_rules(inv, [...])`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent_governance.audit.scanner import Inventory
from agent_governance.checks import CheckResult, Severity, fingerprint


@dataclass(frozen=True)
class Finding:
    """A single audit finding."""

    rule_id: str
    severity: Severity
    title: str
    detail: str
    recommendation: str
    file: str | None = None
    line: int | None = None
    where: str | None = None  # human-readable location summary

    def location(self) -> str:
        if self.file and self.line:
            return f"{self.file}:{self.line}"
        if self.file:
            return self.file
        if self.where:
            return self.where
        return "(repo-wide)"

    def to_check_result(self) -> CheckResult:
        """Bridge to the runtime sink protocol so audit findings can flow
        through GitHubIssueSink. Detail bundles location + recommendation."""
        detail = f"[{self.location()}] {self.title}. {self.recommendation}"
        return CheckResult(
            check_name=f"audit.{self.rule_id.lower()}",
            fired=True,
            severity=self.severity,
            detail=detail[:1024],
            fingerprint=fingerprint("audit", self.rule_id, self.location()),
        )


class Rule(Protocol):
    rule_id: str
    severity: Severity

    def check(self, inv: Inventory) -> list[Finding]: ...


# ---------------------------------------------------------------------------
# Concrete rules
# ---------------------------------------------------------------------------


@dataclass
class LLMCallNoEvalRule:
    rule_id: str = "LLM_CALL_NO_EVAL"
    severity: Severity = "warning"

    def check(self, inv: Inventory) -> list[Finding]:
        if not inv.llm_calls:
            return []
        if inv.eval_paths:
            return []
        sample = inv.llm_calls[0]
        return [
            Finding(
                rule_id=self.rule_id,
                severity=self.severity,
                title="LLM calls present but no eval setup detected",
                detail=(
                    f"Found {len(inv.llm_calls)} LLM call site(s) "
                    "but no evals/ directory, golden set, or eval framework "
                    "(evalkit, ragas, promptfoo, deepeval) was imported. Without evals, "
                    "you have no automated way to know whether a prompt or model change "
                    "made the agent better or worse."
                ),
                recommendation=(
                    "Add a golden set of representative inputs + expected outputs "
                    "and run an LLM-judge over them. evalkit and promptfoo are both "
                    "low-overhead starting points."
                ),
                file=sample.file,
                line=sample.line,
            )
        ]


@dataclass
class LLMCallNoTraceRule:
    rule_id: str = "LLM_CALL_NO_TRACE"
    severity: Severity = "warning"

    def check(self, inv: Inventory) -> list[Finding]:
        if not inv.llm_calls:
            return []
        if inv.trace_providers:
            return []
        sample = inv.llm_calls[0]
        return [
            Finding(
                rule_id=self.rule_id,
                severity=self.severity,
                title="LLM calls present but no tracing provider wired",
                detail=(
                    f"Found {len(inv.llm_calls)} LLM call site(s) "
                    "but no Langfuse / LangSmith / OpenTelemetry / Phoenix / Helicone "
                    "import was detected. Without tracing, production debugging means "
                    "asking 'what did the model see?' with no answer."
                ),
                recommendation=(
                    "Wire Langfuse or LangSmith (cheapest path) so every request "
                    "produces a trace with token counts, latency, and prompt context. "
                    "Then return trace_id in your API response so users can attach "
                    "feedback to specific runs."
                ),
                file=sample.file,
                line=sample.line,
            )
        ]


@dataclass
class MissingEnvExampleRule:
    rule_id: str = "MISSING_ENV_EXAMPLE"
    severity: Severity = "info"

    def check(self, inv: Inventory) -> list[Finding]:
        if inv.env_example_present:
            return []
        return [
            Finding(
                rule_id=self.rule_id,
                severity=self.severity,
                title="No .env.example committed",
                detail=(
                    "Repo has no `.env.example` documenting the env vars the app expects. "
                    "New contributors and operators have to grep config.py to figure out "
                    "what to set, which is a setup-time tax."
                ),
                recommendation=(
                    "Commit a `.env.example` listing every variable with a safe placeholder "
                    "(`ANTHROPIC_API_KEY=`, `LANGFUSE_PUBLIC_KEY=`, …). Add it to README's "
                    "Quick-start. The real `.env` stays gitignored."
                ),
                where=".env.example",
            )
        ]


@dataclass
class HardcodedSecretRule:
    rule_id: str = "HARDCODED_API_KEY"
    severity: Severity = "violation"

    def check(self, inv: Inventory) -> list[Finding]:
        out: list[Finding] = []
        for file, line, kind in inv.hardcoded_secret_hits:
            out.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=self.severity,
                    title=f"Possible hardcoded {kind} in source",
                    detail=(
                        f"A string in `{file}:{line}` matches the format of a {kind}. "
                        "If real, this is a credential leak the moment the repo is published."
                    ),
                    recommendation=(
                        "Move the value to an environment variable read via pydantic-settings "
                        "(or equivalent). Rotate the secret immediately if it was ever real. "
                        "Add a pre-commit hook (`detect-secrets`) to catch the next one."
                    ),
                    file=file,
                    line=line,
                )
            )
        return out


@dataclass
class MissingGovernanceNodeRule:
    rule_id: str = "MISSING_GOVERNANCE_NODE"
    severity: Severity = "warning"

    def check(self, inv: Inventory) -> list[Finding]:
        if not inv.llm_calls:
            return []
        if inv.governance_node_wired:
            return []
        return [
            Finding(
                rule_id=self.rule_id,
                severity=self.severity,
                title="No model-output governance layer wired",
                detail=(
                    f"Found {len(inv.llm_calls)} LLM call site(s) but no "
                    "`agent_governance.build_governance_node` import. There is no explicit "
                    "place gating model output for disclaimer enforcement, URL allowlists, "
                    "prompt-injection observability, or any other policy."
                ),
                recommendation=(
                    "Add `agent-governance` as a dependency and wire a governance step "
                    "after your final LLM call (or as a LangGraph node before END). "
                    "Start with DisclaimerCheck + URLAllowlistCheck + PromptInjectionCheck. "
                    "See https://github.com/odanree/agent-governance for the integration guide."
                ),
                where="(repo-wide)",
            )
        ]


@dataclass
class MissingProvenanceDisclaimerRule:
    rule_id: str = "MISSING_PROVENANCE_DISCLAIMER"
    severity: Severity = "warning"

    def check(self, inv: Inventory) -> list[Finding]:
        # Only fires when the prompt actually mentions synthetic/illustrative data.
        prompts_using_synthetic = [p for p in inv.prompts if p.has_provenance_language]
        if not prompts_using_synthetic:
            return []
        if inv.governance_node_wired:
            return []  # DisclaimerCheck likely covers it; rule is conservative.
        p = prompts_using_synthetic[0]
        return [
            Finding(
                rule_id=self.rule_id,
                severity=self.severity,
                title="Prompt references synthetic/illustrative data with no enforcement",
                detail=(
                    f"Prompt `{p.name}` at `{p.file}:{p.line}` uses provenance language "
                    "(synthetic / illustrative / disclaimer / paywalled / not authoritative), "
                    "but no DisclaimerCheck or equivalent runtime guard was detected. "
                    "If the model drops the disclaimer the prompt asks for, you have no "
                    "structural backstop — the agent will silently present non-authoritative "
                    "data as authoritative."
                ),
                recommendation=(
                    "Wire `agent_governance.DisclaimerCheck(canonical=...)` into a governance "
                    "node. Set the canonical line to the exact text you want appended when "
                    "the model drops the disclaimer; the check pattern-matches the model's own "
                    "phrasing so it only appends when truly missing."
                ),
                file=p.file,
                line=p.line,
            )
        ]


@dataclass
class MissingUrlOutputValidationRule:
    rule_id: str = "MISSING_URL_OUTPUT_VALIDATION"
    severity: Severity = "warning"

    def check(self, inv: Inventory) -> list[Finding]:
        if not inv.url_response_fields:
            return []
        if inv.has_url_allowlist_check:
            return []
        file, line = inv.url_response_fields[0]
        return [
            Finding(
                rule_id=self.rule_id,
                severity=self.severity,
                title="API responses surface URLs with no allowlist validation",
                detail=(
                    f"Response model in `{file}:{line}` exposes a `url` field but no "
                    "URLAllowlistCheck was detected in the repo. If the URL comes from "
                    "LLM output (directly or via a tool that the LLM influenced), an "
                    "hallucinated host can ship to users."
                ),
                recommendation=(
                    "Add `agent_governance.URLAllowlistCheck(allowlist=[…])` to a governance "
                    "node, listing the hostnames you actually want to expose (your own "
                    "domain, the upstream canonical sources). Anything else gets redacted."
                ),
                file=file,
                line=line,
            )
        ]


@dataclass
class PromptLacksRefusalLanguageRule:
    rule_id: str = "PROMPT_LACKS_REFUSAL_LANGUAGE"
    severity: Severity = "info"

    def check(self, inv: Inventory) -> list[Finding]:
        # Only flag prompts that are long enough to plausibly include rules.
        candidates = [p for p in inv.prompts if p.length >= 300 and not p.has_refusal_language]
        if not candidates:
            return []
        p = candidates[0]
        return [
            Finding(
                rule_id=self.rule_id,
                severity=self.severity,
                title=f"Long prompt `{p.name}` has no refusal / boundary language",
                detail=(
                    f"The prompt at `{p.file}:{p.line}` is {p.length} chars long but contains "
                    "no occurrences of 'do not', 'never', 'must not', 'refuse', or similar "
                    "boundary tokens. Long prompts with no negative constraints often "
                    "underspecify what the model should refuse — leading to over-eager "
                    "answers and hallucinated guidance."
                ),
                recommendation=(
                    "Add explicit refusal rules: 'If the facts do not contain the answer, "
                    "say so', 'Never invent URLs / phone numbers / dosages', "
                    "'Do not characterize transfers as arm's-length or inter-family', etc. "
                    "Use numbered RULES blocks so the eval can spot-check each one."
                ),
                file=p.file,
                line=p.line,
            )
            for p in candidates[:1]  # one per prompt to avoid noise
        ]


@dataclass
class MissingTraceIdInResponseRule:
    rule_id: str = "MISSING_TRACE_ID_IN_RESPONSE"
    severity: Severity = "info"

    def check(self, inv: Inventory) -> list[Finding]:
        if not inv.trace_providers:
            return []
        if not inv.api_response_models:
            return []
        if inv.trace_id_in_response:
            return []
        file, line = inv.api_response_models[0]
        return [
            Finding(
                rule_id=self.rule_id,
                severity=self.severity,
                title="Tracing is enabled but trace_id is not returned to callers",
                detail=(
                    f"Repo imports {sorted(inv.trace_providers)} but no API response model "
                    "(found at `{file}:{line}`) carries a `trace_id` field. Without it, a user "
                    "reporting 'this answer was wrong' has no way to point you at the specific "
                    "trace, and feedback can't round-trip back to the trace as a score."
                ),
                recommendation=(
                    "Add `trace_id: str | None = None` to your top-level response model "
                    "and populate it from your tracing SDK at request time. Surface it in "
                    "error responses too — bad answers are the ones you want to debug."
                ),
                file=file,
                line=line,
            )
        ]


# ---------------------------------------------------------------------------
# Registry + runner
# ---------------------------------------------------------------------------


ALL_RULES: list[Rule] = [
    LLMCallNoEvalRule(),
    LLMCallNoTraceRule(),
    MissingEnvExampleRule(),
    HardcodedSecretRule(),
    MissingGovernanceNodeRule(),
    MissingProvenanceDisclaimerRule(),
    MissingUrlOutputValidationRule(),
    PromptLacksRefusalLanguageRule(),
    MissingTraceIdInResponseRule(),
]


def run_rules(inv: Inventory, rules: list[Rule] | None = None) -> list[Finding]:
    """Run rules in order and return all findings, flattened."""
    findings: list[Finding] = []
    for r in (rules or ALL_RULES):
        try:
            findings.extend(r.check(inv))
        except Exception:  # pragma: no cover - rule bugs shouldn't kill the audit
            import logging
            logging.getLogger(__name__).exception("audit rule %s raised", r.rule_id)
    return findings
