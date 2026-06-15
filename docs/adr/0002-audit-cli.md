# ADR-0002: Add a `audit` CLI alongside the runtime checks

- **Status:** Accepted
- **Date:** 2026-06-15
- **Deciders:** Danh Le
- **Relates to:** [ADR-0001](0001-design.md)

## Context

[v0.1](../../CHANGELOG.md#010--2026-06-14) shipped the runtime half of governance — a LangGraph node + sinks + checks that *enforce* policy on model output at request time. That's the right answer to "we already know we need governance; install something."

The complementary question — **"does this LLM agent repo need governance, and where?"** — wasn't covered. That question matches a different role:

- **Runtime checks** answer to an engineer wiring up an agent.
- **An auditor** answers to a reviewer (or consultant) walking into a repo cold and asking: where's the eval gate, where's the trace surface, where are the unguarded LLM calls, where does this prompt encourage hallucination?

Both jobs need to exist. The question is whether they live in one package or two.

## Decision

**One package, two faces.** Ship an `audit` subcommand that scans a Python repo and reports governance gaps, recommending the runtime checks that already ship in the same package to close them.

- `from agent_governance import build_governance_node, URLAllowlistCheck` — runtime, what you *install*.
- `agent-governance audit ./some-repo` — static analysis, what you *run on a repo* to find gaps.

Single dependency. Single README. A recommendation in the audit output (`add agent_governance.URLAllowlistCheck`) lands the reviewer in the exact same package that printed it.

### What the auditor does

1. **Scanner** walks `.py` files (skipping `.venv`, `node_modules`, `dist`, etc.) and builds an `Inventory`:
   - LLM call sites (AST: which SDK, where).
   - Prompt locations (named constants + long string literals; whether they contain refusal / provenance language).
   - Eval setups (`evals/`, golden files, `evalkit` / `ragas` / `promptfoo` / `deepeval` imports).
   - Trace providers (`langfuse`, `langsmith`, `opentelemetry`, `phoenix`, `helicone` imports).
   - Pydantic response models — does any expose a `url` field? a `trace_id` field?
   - LangGraph usage, agent-governance import, `.env.example` presence, hardcoded-secret regex hits.
2. **Rules** read the inventory and emit `Finding`s. Each rule is a `Protocol`-conforming object; the registry `ALL_RULES` is the default but users can pass `run_rules(inv, [...custom...])`.
3. **Report formatters** turn findings into Markdown (for humans, paste into PR) or JSON (for CI).
4. **CLI** (`agent-governance audit`) glues them together, with `--fail-on=warning` for CI gating and `--github OWNER/REPO` to file findings as deduplicated issues via the existing `GitHubIssueSink`.

### Rule set shipped in v0.2

Nine rules: `LLM_CALL_NO_EVAL`, `LLM_CALL_NO_TRACE`, `MISSING_ENV_EXAMPLE`, `HARDCODED_API_KEY`, `MISSING_GOVERNANCE_NODE`, `MISSING_PROVENANCE_DISCLAIMER`, `MISSING_URL_OUTPUT_VALIDATION`, `PROMPT_LACKS_REFUSAL_LANGUAGE`, `MISSING_TRACE_ID_IN_RESPONSE`. Each carries a `recommendation` that points at a concrete runtime check (or a small ADR-worthy decision) — the reviewer doesn't get a checklist with no guidance.

### Conservative defaults

False positives waste a reviewer's time and erode trust in the tool. False negatives just mean a finding wasn't caught. The rule set is biased toward the latter:

- LLM call detection requires both an SDK import AND a Call expression whose leaf name is a known LLM method (`create`, `invoke`, `ainvoke`, …). A stray import alone doesn't trigger anything.
- Prompts are flagged only when named via a convention (`_SYSTEM`, `_PROMPT`, …) or longer than 200 chars. Short throwaway strings don't pollute the inventory.
- The "missing governance" rule only fires when LLM calls were actually detected.
- The provenance rule only fires when a prompt mentions synthetic/illustrative data — silent on agents that don't have that risk surface.

## Consequences

**Positive**
- Reuse story is tight: `audit` outputs sentences like "wire `agent_governance.URLAllowlistCheck`," which is a real symbol in the same install. No "go install some other library" indirection.
- One README, one ADR set, one CHANGELOG. Reviewers don't context-switch between packages to evaluate the work.
- The audit produces a CI artifact (`--format=json` + `--fail-on`) and a tracker artifact (`--github`). Both reuse the existing v0.1 plumbing.
- Demoable: run the CLI on real portfolio repos, paste the report. The work justifies itself.

**Negative**
- Package surface grows. Newcomers see `agent_governance.checks` AND `agent_governance.audit` and have to learn which is which. Mitigated by clear README sections and the fact that the CLI is a separate `agent-governance audit ...` entry point — most installs only call one of the two.
- AST-based static analysis is heuristic. A maintainer who renames `_SYSTEM` to `_RULES` and writes their LLM call through a wrapper class will fool the scanner. Acceptable for an opinionated v0.2; future work could add a config file letting projects whitelist additional patterns.
- Issue spam risk if someone runs `--github` against a real repo with many findings. Mitigated by the same fingerprint-based dedup the runtime sink uses — a second `audit` run on an unchanged repo adds comments, not new issues.

## Alternatives considered

- **Separate `agent-governance-audit` repo.** Clean separation. Rejected because the audit's main value is the recommendations, which point right back at this package. Splitting would require either the audit depending on this package (still effectively one install) or duplicating the recommendation text (drift risk).
- **Claude Code subagent (`/governance-audit`).** Like the `privacy-scrub` pattern. Real value, but invisible in the package's GitHub surface. Could be added on top of this CLI later.
- **Skip the auditor entirely; runtime checks only.** What v0.1 shipped. Reviewing the actual JD ("Audit AI tools and practices across data, models and software engineering") made it clear the audit half is the load-bearing one for the consulting framing — the runtime half is the implementation half.
- **Larger rule set with semantic checks** (e.g. LLM-judge an existing prompt). Possible but expensive; v0.2 stays at regex/AST. Future work could add an optional `--with-llm-judge` mode for rules that need it.

## Follow-ups

- Per-project config file (`.agent-governance.toml`) letting projects pin known-okay patterns and add custom prompt-name conventions.
- More rules: `MISSING_PII_REDACTION_AT_TRACE`, `PROMPT_INJECTION_NOT_LOGGED`, `EVAL_HAS_NO_REFUSAL_CASES`, `MODEL_VERSION_HARDCODED_LITERAL`.
- Optional `--with-llm-judge` mode that runs a small Claude prompt over each found prompt and asks "would this prompt fail a safety review?" — gated behind an API key the user supplies.
- Demo GitHub Action that runs `agent-governance audit --fail-on=warning` on PR.

## Links

- Demo reports against real repos: [docs/demo/audit-oc-realestate-intel.md](../demo/audit-oc-realestate-intel.md), [docs/demo/audit-clinical-mcp-server.md](../demo/audit-clinical-mcp-server.md)
- Implementation: `src/agent_governance/audit/` (scanner, rules, report, cli)
- Runtime checks the audit recommends installing: `src/agent_governance/checks.py` (see [ADR-0001](0001-design.md))
