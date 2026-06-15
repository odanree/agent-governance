"""The governance node — runs checks, threads mutations, dispatches to sink.

Framework-agnostic: it's just `async def node(state: dict) -> dict`. Drop into
LangGraph as a node, into LangChain as a `RunnableLambda`, or call it as a
plain async function in any other pipeline.

Output contract:
    {
        "answer": str,                  # possibly mutated by checks
        "governance_report": [          # one entry per check that ran
            {
                "check_name": str,
                "fired": bool,
                "severity": "info" | "warning" | "violation",
                "detail": str,
                "fingerprint": str,
                "mutated_answer": bool,
                "new_answer": str | None,
            },
            ...
        ],
    }
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Awaitable, Callable, Iterable

from agent_governance.checks import Check, CheckResult
from agent_governance.observability import (
    NullObservabilityAdapter,
    ObservabilityAdapter,
)
from agent_governance.sinks import IncidentSink, LogSink

log = logging.getLogger(__name__)


def build_governance_node(
    checks: Iterable[Check],
    sink: IncidentSink | None = None,
    observability: ObservabilityAdapter | None = None,
    *,
    score_prefix: str = "governance_",
) -> Callable[[dict], Awaitable[dict]]:
    """Return an async node function bound to a check registry, sink, and
    observability adapter.

    Args:
        checks: ordered iterable of checks to run. Order matters — checks
            that mutate `state["answer"]` are visible to subsequent checks.
        sink: where to report fired checks. Defaults to LogSink.
        observability: trace + score adapter. Defaults to NullObservabilityAdapter.
        score_prefix: prefix for Langfuse/etc score names. Each check
            produces a 0/1 score named `<prefix><check_name>` so you can
            filter "scores.governance_disclaimer = 1" in your dashboard.
    """
    check_list = list(checks)
    incident_sink: IncidentSink = sink or LogSink()
    obs: ObservabilityAdapter = observability or NullObservabilityAdapter()

    async def _node(state: dict) -> dict:
        report: list[dict] = []
        answer = state.get("answer") or ""

        for check in check_list:
            try:
                # Each check sees the latest answer (post any prior mutation).
                result = await check.run({**state, "answer": answer})
            except Exception as e:
                log.exception("governance: check %s raised %s", check.name, e)
                continue

            report.append(asdict(result))
            _emit_score(obs, score_prefix, result)

            if result.mutated_answer and result.new_answer is not None:
                answer = result.new_answer

            if result.fired:
                _dispatch_async(incident_sink, result, obs.get_trace_id())

        try:
            obs.tag_trace(
                tags=[f"governance:{r['check_name']}" for r in report if r["fired"]],
                metadata={"governance_fired": sum(1 for r in report if r["fired"])},
            )
        except Exception as e:  # pragma: no cover - adapter shouldn't raise
            log.warning("governance: tag_trace failed: %s", e)

        return {"answer": answer, "governance_report": report}

    return _node


def _emit_score(
    obs: ObservabilityAdapter,
    prefix: str,
    result: CheckResult,
) -> None:
    """Per-check 0/1 score, only when a trace is active. Adapter errors don't
    propagate — observability is best-effort by contract."""
    try:
        trace_id = obs.get_trace_id()
        if trace_id:
            obs.create_score(
                trace_id=trace_id,
                name=f"{prefix}{result.check_name}",
                value=1.0 if result.fired else 0.0,
                comment=result.detail,
            )
    except Exception as e:  # pragma: no cover
        log.warning("governance: create_score failed: %s", e)


def _dispatch_async(sink: IncidentSink, result: CheckResult, trace_id: str | None) -> None:
    """Fire-and-forget sink dispatch. Sink failures never block the user
    response. Falls back to a synchronous report if there's no running loop
    (e.g. called from a sync test) — but this is rare in practice."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(sink.report(result, trace_id))
    except RuntimeError:
        # No running loop — block briefly. Tests that opt into this path
        # should normally use the async path or NullSink.
        asyncio.run(sink.report(result, trace_id))
