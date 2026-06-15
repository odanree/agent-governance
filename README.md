# agent-governance

A small, framework-agnostic governance layer for LLM agents: **runs policy checks on each model output, mutates the answer when needed, and reports incidents to a configurable sink** — including auto-filed GitHub issues, deduplicated by fingerprint.

Drop into a LangGraph node, a LangChain Runnable, a FastAPI dependency, or call it as a plain async function. The package never imports any LLM SDK or observability vendor.

```
                         ┌─────────────────────┐
                         │ governance_node     │
state ─────────────────► │                     │ ─────► state'
                         │  ┌── checks ──────┐ │        (answer may be
                         │  │ DisclaimerCheck│ │        mutated; report
                         │  │ URLAllowlist   │ │        attached)
                         │  │ PromptInjection│ │
                         │  │ <your own>     │ │        ┌─ sink ────────┐
                         │  └────────────────┘ │ ──────►│ LogSink       │
                         │  fired? ────────────┼────────►│ GitHubSink    │
                         └─────────────────────┘        │ NullSink      │
                                                       │ <your own>    │
                                                       └───────────────┘
```

## Install

```bash
pip install git+https://github.com/odanree/agent-governance.git@main
# or pin to a tag: ...@v0.1.0
```

Requires Python 3.11+.

## Quick start (LangGraph)

```python
from agent_governance import (
    DisclaimerCheck, URLAllowlistCheck, PromptInjectionCheck,
    build_governance_node, build_sink,
)

DISCLAIMER = "*Owner data is synthetic — not from authoritative records.*"

node = build_governance_node(
    checks=[
        DisclaimerCheck(canonical=DISCLAIMER),
        URLAllowlistCheck(allowlist=["assessor.ocgov.com"]),
        PromptInjectionCheck(),
    ],
    sink=build_sink(
        "github",
        github_repo="myorg/my-agent",
        github_token=os.environ["GITHUB_TOKEN"],
    ),
)

# Drop into a LangGraph StateGraph:
g.add_node("governance", node)
g.add_edge("summarize", "governance")
g.add_edge("governance", END)
```

The node reads `state["answer"]` (and optionally `state["query"]`, `state["provenance"]`, etc — depends on the checks), returns:

```python
{
    "answer": str,                  # possibly mutated
    "governance_report": [
        {
            "check_name": "disclaimer",
            "fired": True,
            "severity": "warning",
            "detail": "model dropped disclaimer; canonical appended",
            "fingerprint": "ab12cd34ef56",
            "mutated_answer": True,
            "new_answer": "<the appended text>",
        },
        ...
    ],
}
```

## Checks

All checks implement:

```python
class Check(Protocol):
    name: str
    async def run(self, state: dict) -> CheckResult: ...
```

Three ship in v0.1:

### `DisclaimerCheck` — enforce an italic provenance disclaimer

When `state["provenance"]["disclaimer"]` is truthy and the answer doesn't already contain an italic span matching the pattern, append the canonical text. Configurable via `canonical=`, `pattern=`, `state_key=`.

Use case: your agent answers with synthetic or non-authoritative data and the system prompt instructs it to add an italic note. This is the structural backstop for when the model drops it.

### `URLAllowlistCheck` — redact non-allowlisted URLs

Extracts URLs from the answer (including bare TLD hostnames like `ocassessor.gov` that models love to hallucinate). Any URL whose hostname isn't on `allowlist` is replaced with `[URL removed by governance: not on allowlist]`. Same offending host across requests produces the same fingerprint so recurring hallucinations dedupe.

### `PromptInjectionCheck` — observability for injection attempts

Read-only. Scans `state["query"]` for known injection patterns (`ignore previous instructions`, `you are now`, `<|im_start|>`, …) and reports an `info` incident when matched. Never mutates state — the goal is to track patterns over time, not to refuse the request.

### Custom checks

Anything implementing the Protocol works. The check just receives the current state (with `answer` reflecting any prior check's mutation) and returns a `CheckResult`.

## Sinks

```python
class IncidentSink(Protocol):
    async def report(self, result: CheckResult, trace_id: str | None) -> None: ...
```

Four built-in:

| Sink | When fired | External side effects |
|---|---|---|
| `LogSink` | Structured `WARNING` log line | None |
| `NullSink` | Drops silently | None |
| `GitHubIssueSink` | Files or comments on a GitHub issue, dedup by fingerprint label | GitHub API call |
| `build_sink(kind, ...)` | Factory — picks one of the above by string | Same as picked sink |

### `GitHubIssueSink` — dedup + privacy contract

Issues get labeled `governance`, `governance:<check_name>`, and `governance:<check_name>:<fingerprint>`. On a second occurrence of the same fingerprint, the sink finds the open issue by the fingerprint label and **adds a comment** rather than opening a new one. Recurring violations become one ticket with a counter.

**Privacy.** Issue bodies carry only safe metadata: check name, short detail (e.g. "redacted 1 non-allowlisted URL: ocassessor.gov"), `trace_id`, severity, fingerprint, timestamp. They **never** carry the raw user query or the full answer — both are PII surfaces. To inspect the full context, look up `trace_id` in your tracing backend, where access is auth-gated.

**Failure mode.** Sink errors (network down, rate-limited, bad token) are caught and logged. They never propagate to the host — your user response is not blocked on GitHub being reachable.

## Observability

The node takes an optional `ObservabilityAdapter` so it can emit per-check scores and trace tags to whatever tracing backend you already use:

```python
class ObservabilityAdapter(Protocol):
    def get_trace_id(self) -> str | None: ...
    def tag_trace(self, tags: list[str], metadata: dict | None = None) -> None: ...
    def create_score(self, trace_id: str, name: str, value: float, comment: str | None = None) -> None: ...
```

Provide your own — a one-page wrapper around Langfuse, LangSmith, OpenTelemetry, etc. — or leave it unset and `NullObservabilityAdapter` will no-op everything. See the [oc-realestate-intel integration](https://github.com/odanree/oc-realestate-intel/blob/master/app/governance.py) for a Langfuse adapter example.

When wired, each check produces a 0/1 score named `<prefix><check_name>` (prefix defaults to `governance_`). Filtering `scores.governance_disclaimer = 1` in your dashboard surfaces every real fire across production traffic.

## Design

See [docs/adr/0001-design.md](docs/adr/0001-design.md) for the load-bearing decisions: why checks and sinks are separate Protocols, why issues dedupe by fingerprint, why issue bodies omit user-supplied content, why the node uses fire-and-forget dispatch instead of awaiting the sink.

## Status

v0.1 — small, opinionated, used in production by [oc-realestate-intel](https://github.com/odanree/oc-realestate-intel). API is not yet considered stable; breaking changes will land in 0.x minors with notes in [CHANGELOG.md](CHANGELOG.md).

## License

MIT
