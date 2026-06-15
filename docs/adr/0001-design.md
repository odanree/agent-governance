# ADR-0001: agent-governance design

- **Status:** Accepted
- **Date:** 2026-06-14
- **Deciders:** Danh Le

## Context

LLM agents need a place to enforce policy on model output (disclaimers, URL allowlists, output redaction, length caps) and a record of when policy fired so the team can triage drift. This work started as inline helpers inside [oc-realestate-intel](https://github.com/odanree/oc-realestate-intel) and was extracted into a standalone package once a second agent (Beacon, future agents) wanted the same capability.

Two anti-patterns shaped the design:

- **Inline guards.** Burying policy inside a `summarize_node` makes it invisible in traces and impossible to reuse. We want one named place a reviewer can point at.
- **Vendor-coupled guardrails.** Many guardrails libraries hard-bind to a specific LLM provider, framework, or observability vendor. We want to drop into LangGraph today, LangChain tomorrow, and plain async pipelines after that — without ripping out the package.

## Decision

A small package with three abstractions, each behind a Protocol:

1. **`Check`** — `async def run(state: dict) -> CheckResult`. The state dict is whatever shape the host uses; checks read the keys they care about. `CheckResult` carries the verdict (fired / severity / detail / fingerprint) and, optionally, a `new_answer` that the node will adopt before running subsequent checks.
2. **`IncidentSink`** — `async def report(result, trace_id) -> None`. Where fired checks get reported. The package ships `LogSink`, `NullSink`, `GitHubIssueSink`; hosts can add their own (Slack, PagerDuty, queue).
3. **`ObservabilityAdapter`** — three hooks (`get_trace_id`, `tag_trace`, `create_score`). The package never imports Langfuse / LangSmith / OTel directly. Hosts wrap their tracing backend in an adapter; tests use `NullObservabilityAdapter`.

The node is a function factory: `build_governance_node(checks, sink, observability)` returns an `async def node(state) -> state'` that hosts wire into their pipeline.

### Why dedup by fingerprint label

`GitHubIssueSink` dedups by attaching a label `governance:<check>:<fingerprint>` to each issue and, on subsequent fires, searching for that label before posting. Fingerprints are SHA-256(check_name + stable fingerprint inputs), truncated to 12 chars — short enough for label UX, long enough to avoid collisions in a single repo's lifetime.

The alternative — dedup by issue title — fails the moment a check's detail string varies (e.g. "redacted 1 URL" vs "redacted 2 URLs"). Labels are stable.

### Why fire-and-forget dispatch

The sink call uses `asyncio.create_task(sink.report(...))`. The user's request must not block on GitHub being reachable. Sink failures are logged inside `GitHubIssueSink.report` itself and never propagate.

Tradeoff: a request can complete before its sink dispatch lands. For sinks that need durability (a sink writing to a durable queue, say), the host can wrap the sink in their own `await`-on-shutdown pattern. The default path optimizes for "agent stays up when the tracker is down."

### Why issue bodies omit raw query and answer

LLM agents are often used in privacy-sensitive contexts. A user query can contain PII that the host didn't anticipate (SSN typed into a chat, email in a comparison query). Posting that to a public-ish issue tracker is a leak the package would be responsible for.

Issue bodies carry: check name, severity, short `detail` (which the check author writes; checks are responsible for keeping `detail` safe — the URL host name is OK, the full URL with a customer ID is not), `trace_id`, fingerprint, timestamp. To inspect the full context, look up the `trace_id` in your tracing backend, where access is auth-gated.

## Consequences

**Positive**
- One named gate. "Where does this team gate model output?" — `governance_node`.
- New checks are a 30-line file. No surgery on the host's pipeline.
- Sinks are pluggable: dev = `LogSink`, prod opts in to `GitHubIssueSink`. Same code path.
- Dedup means production incidents don't drown the tracker.

**Negative**
- The state-dict-as-contract has no type safety. Checks that read `state["provenance"]["disclaimer"]` blow up if the host changes the shape. Mitigated by the optional `state_key=` parameter on built-in checks; full typing would force a host-coupled state schema, which is worse than the runtime risk.
- The `ObservabilityAdapter` Protocol only has three methods. Hosts that want more (custom span events, child observations) have to bypass the package and call their backend directly. Accepted because adding methods drives toward vendor coupling.

## Alternatives considered

- **Use a third-party guardrails library** (NeMo Guardrails, Llama Guard). Rejected for v0.1 because they add a model dependency for what is, today, three regex/string checks. Worth revisiting when a check needs semantic reasoning ("is this answer biased?", "did the answer reveal a PII field the prompt asked us to redact?").
- **Build into the host repo as a module.** That's where this started. Extracted once a second consumer (Beacon, future agents) appeared — the cost of the package boundary is paid back the moment the second host integrates.
- **Sync sink with retry queue.** Considered. The added durability isn't worth the latency and complexity in v0.1. Hosts that need it can wrap their own sink.

## Follow-ups

- `LengthCapCheck` (answer-length sanity).
- `PIIRedactorCheck` (regex-based SSN/email/phone redaction).
- A `SlackSink` and `QueueSink` once a host asks for them.
- Drop the runtime `httpx` dependency in favor of an injectable HTTP client so hosts that already wire one (e.g. with retries, opentelemetry) don't pull in a second.
