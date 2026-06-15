# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-06-15

### Added
- `agent-governance audit` CLI: static analysis of any Python LLM agent repo for governance gaps. Walks the repo, builds an Inventory of LLM call sites + prompts + evals + tracing + configs, runs a registry of 9 rules, prints a Markdown or JSON report. See [ADR-0002](docs/adr/0002-audit-cli.md).
- 9 rules at launch: `LLM_CALL_NO_EVAL`, `LLM_CALL_NO_TRACE`, `MISSING_ENV_EXAMPLE`, `HARDCODED_API_KEY`, `MISSING_GOVERNANCE_NODE`, `MISSING_PROVENANCE_DISCLAIMER`, `MISSING_URL_OUTPUT_VALIDATION`, `PROMPT_LACKS_REFUSAL_LANGUAGE`, `MISSING_TRACE_ID_IN_RESPONSE`. Each carries a `recommendation` that points at a concrete runtime check (or a small ADR-worthy decision).
- `--fail-on=violation|warning|info` for CI gating.
- `--github OWNER/REPO` to file findings as deduplicated GitHub issues via the existing `GitHubIssueSink` — same fingerprint-based dedup as the runtime sinks.
- `--format=json` for machine consumption.
- Demo runs against [oc-realestate-intel](docs/demo/audit-oc-realestate-intel.md) and [clinical-mcp-server](docs/demo/audit-clinical-mcp-server.md) — both surface real findings the maintainer hadn't tracked yet.
- 36 new audit tests (76 total passing); coverage spans scanner, every rule, both formatters, and CLI exit-code behavior.

### Notes
- Package surface now has two faces: runtime checks for engineers wiring an agent (`from agent_governance import ...`), and the audit CLI for reviewers/consultants walking into a repo cold (`agent-governance audit ./repo`). They share the sink layer (GitHub issues) and the recommendation text.
- AST-based static analysis is heuristic. False-positive avoidance is prioritized; see ADR-0002 for the design rationale.

## [0.1.0] — 2026-06-14

### Added
- `governance_node` factory (`build_governance_node`) for LangGraph / any async pipeline.
- Three built-in checks: `DisclaimerCheck`, `URLAllowlistCheck`, `PromptInjectionCheck`.
- Four incident sinks: `LogSink` (default), `NullSink`, `GitHubIssueSink` (deduplicated by fingerprint label), and a `build_sink` factory.
- `ObservabilityAdapter` Protocol + `NullObservabilityAdapter` default so the package never imports a tracing vendor.
- Privacy contract: `GitHubIssueSink` issue bodies omit raw user query and full answer; trace id is the indirection.
- Extracted from the in-tree governance node in [oc-realestate-intel#1](https://github.com/odanree/oc-realestate-intel/pull/1)'s follow-up.

### Notes
- v0.x API is provisional. Breaking changes will land in minor bumps with notes here.

[Unreleased]: https://github.com/odanree/agent-governance/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/odanree/agent-governance/releases/tag/v0.2.0
[0.1.0]: https://github.com/odanree/agent-governance/releases/tag/v0.1.0
