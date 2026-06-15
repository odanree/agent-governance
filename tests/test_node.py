"""Tests for the governance node — registry orchestration, mutation threading,
score emission, sink dispatch."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from agent_governance import (
    CheckResult,
    DisclaimerCheck,
    NullObservabilityAdapter,
    NullSink,
    URLAllowlistCheck,
    build_governance_node,
)


CANONICAL = "*synthetic*"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class RecordingObs(NullObservabilityAdapter):
    trace_id: str | None = "trace-test"
    scores: list[tuple[str, float, str | None]] = field(default_factory=list)
    tagged: list[list[str]] = field(default_factory=list)
    metadata: list[dict] = field(default_factory=list)

    def get_trace_id(self) -> str | None:
        return self.trace_id

    def tag_trace(self, tags, metadata=None):
        self.tagged.append(list(tags))
        self.metadata.append(dict(metadata or {}))

    def create_score(self, trace_id, name, value, comment=None):
        self.scores.append((name, value, comment))


@dataclass
class RecordingSink:
    reports: list[tuple[CheckResult, str | None]] = field(default_factory=list)

    async def report(self, result, trace_id):
        self.reports.append((result, trace_id))


class StaticCheck:
    """Always returns the given CheckResult."""

    def __init__(self, name: str, result: CheckResult):
        self.name = name
        self._result = result

    async def run(self, state):
        return self._result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_reports_each_check_and_does_not_mutate_when_no_fires():
    obs = RecordingObs()
    sink = RecordingSink()
    node = build_governance_node(
        checks=[DisclaimerCheck(canonical=CANONICAL), URLAllowlistCheck()],
        sink=sink,
        observability=obs,
    )
    out = await node({"answer": "Plain answer.", "query": "hi"})
    assert out["answer"] == "Plain answer."
    assert len(out["governance_report"]) == 2
    assert all(r["fired"] is False for r in out["governance_report"])
    assert sink.reports == []


@pytest.mark.asyncio
async def test_node_threads_mutated_answer_through_subsequent_checks():
    """Mutations are visible to the NEXT check — disclaimer appends, then
    the URL check (run after) sees the appended text."""
    obs = RecordingObs()
    sink = RecordingSink()
    node = build_governance_node(
        checks=[
            DisclaimerCheck(canonical="*synthetic note*"),
            URLAllowlistCheck(),
        ],
        sink=sink,
        observability=obs,
    )
    out = await node(
        {
            "answer": "Answer with no disclaimer.",
            "provenance": {"disclaimer": "*synthetic note*"},
        }
    )
    assert "*synthetic note*" in out["answer"]
    # Disclaimer fired (warning), URL check did not.
    fired = [r for r in out["governance_report"] if r["fired"]]
    assert len(fired) == 1
    assert fired[0]["check_name"] == "disclaimer"


@pytest.mark.asyncio
async def test_node_emits_one_score_per_check_with_prefix():
    obs = RecordingObs()
    node = build_governance_node(
        checks=[DisclaimerCheck(canonical=CANONICAL), URLAllowlistCheck()],
        sink=NullSink(),
        observability=obs,
        score_prefix="gov_",
    )
    await node({"answer": "Plain.", "query": ""})
    names = [s[0] for s in obs.scores]
    assert "gov_disclaimer" in names
    assert "gov_url_allowlist" in names


@pytest.mark.asyncio
async def test_node_skips_scoring_when_no_trace_id():
    obs = RecordingObs(trace_id=None)
    node = build_governance_node(
        checks=[DisclaimerCheck(canonical=CANONICAL)],
        sink=NullSink(),
        observability=obs,
    )
    await node({"answer": "x"})
    assert obs.scores == []


@pytest.mark.asyncio
async def test_node_tags_trace_with_fired_check_names():
    obs = RecordingObs()
    sink = NullSink()
    fired_result = CheckResult(
        check_name="url_allowlist",
        fired=True,
        severity="violation",
        detail="x",
        fingerprint="aa",
    )
    node = build_governance_node(
        checks=[StaticCheck("url_allowlist", fired_result)],
        sink=sink,
        observability=obs,
    )
    await node({"answer": "x"})
    assert obs.tagged[-1] == ["governance:url_allowlist"]
    assert obs.metadata[-1]["governance_fired"] == 1


@pytest.mark.asyncio
async def test_node_dispatches_to_sink_only_for_fired_checks():
    sink = RecordingSink()
    fired = CheckResult(
        check_name="x",
        fired=True,
        severity="violation",
        detail="fire",
        fingerprint="f1",
    )
    passed = CheckResult(
        check_name="y",
        fired=False,
        severity="info",
        detail="pass",
        fingerprint="f2",
    )
    node = build_governance_node(
        checks=[StaticCheck("x", fired), StaticCheck("y", passed)],
        sink=sink,
        observability=RecordingObs(),
    )
    await node({"answer": "x"})
    # Give the create_task a moment to run.
    await asyncio.sleep(0.01)
    assert len(sink.reports) == 1
    assert sink.reports[0][0].check_name == "x"


@pytest.mark.asyncio
async def test_node_check_exception_does_not_break_pipeline():
    """If a check raises, the node logs and continues — one broken check
    must not take down governance for all subsequent checks."""

    class Boom:
        name = "boom"

        async def run(self, state):
            raise RuntimeError("intentional")

    obs = RecordingObs()
    node = build_governance_node(
        checks=[Boom(), DisclaimerCheck(canonical=CANONICAL)],
        sink=NullSink(),
        observability=obs,
    )
    out = await node({"answer": "Plain.", "provenance": {"disclaimer": None}})
    # The Boom check is missing from the report (logged + skipped); the
    # following Disclaimer check still ran.
    names = [r["check_name"] for r in out["governance_report"]]
    assert "disclaimer" in names
    assert "boom" not in names


@pytest.mark.asyncio
async def test_node_default_sink_and_observability_are_safe_when_omitted():
    """Verify the convenience defaults don't blow up on a minimal call."""
    node = build_governance_node(checks=[DisclaimerCheck(canonical=CANONICAL)])
    out = await node({"answer": "x"})
    assert "governance_report" in out


@pytest.mark.asyncio
async def test_node_report_entry_shape():
    """The dict shape of each report entry is the contract MCP callers read."""
    node = build_governance_node(
        checks=[DisclaimerCheck(canonical=CANONICAL)],
        sink=NullSink(),
        observability=NullObservabilityAdapter(),
    )
    out = await node({"answer": "x"})
    entry = out["governance_report"][0]
    assert set(entry.keys()) == {
        "check_name",
        "fired",
        "severity",
        "detail",
        "fingerprint",
        "mutated_answer",
        "new_answer",
    }
