# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/odanree/agent-governance/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/odanree/agent-governance/releases/tag/v0.1.0
