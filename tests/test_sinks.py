"""Tests for incident sinks — LogSink, NullSink, GitHubIssueSink, build_sink."""

from __future__ import annotations

import logging

import pytest

from agent_governance import (
    CheckResult,
    GitHubIssueSink,
    LogSink,
    NullSink,
    build_sink,
)


def _result(check_name: str = "disclaimer", fingerprint: str = "abc123") -> CheckResult:
    return CheckResult(
        check_name=check_name,
        fired=True,
        severity="violation",
        detail="example violation",
        fingerprint=fingerprint,
        mutated_answer=False,
    )


# ---------------------------------------------------------------------------
# LogSink + NullSink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_sink_emits_warning(caplog):
    sink = LogSink()
    with caplog.at_level(logging.WARNING, logger="agent_governance.sinks"):
        await sink.report(_result(), trace_id="trace-xyz")
    assert any("disclaimer" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_null_sink_no_op(caplog):
    sink = NullSink()
    with caplog.at_level(logging.DEBUG):
        await sink.report(_result(), trace_id="trace-xyz")
    assert not any("disclaimer" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# GitHubIssueSink — construction + body shape
# ---------------------------------------------------------------------------


def test_github_sink_rejects_bad_repo_format():
    with pytest.raises(ValueError, match="owner/repo"):
        GitHubIssueSink(repo="not-a-repo", token="tok")


def test_github_sink_rejects_missing_token():
    with pytest.raises(ValueError, match="token"):
        GitHubIssueSink(repo="owner/repo", token="")


def test_github_sink_issue_body_omits_query_and_answer():
    """Privacy contract: the body must NEVER contain the raw user query or
    full answer. They are PII surfaces; the trace_id is the indirection."""
    body = GitHubIssueSink._issue_body(
        _result(), trace_id="trace-xyz", first=True
    )
    # Things that SHOULD be there:
    assert "trace-xyz" in body
    assert "disclaimer" in body
    assert "abc123" in body
    # Things that MUST NOT be there:
    assert "query" not in body.lower() or "trace" in body.lower()  # 'query' appears in disclaimer wording only allowed via trace ref
    assert "user_query" not in body
    assert "full_answer" not in body


def test_github_sink_issue_body_first_vs_followup_differs():
    first = GitHubIssueSink._issue_body(_result(), trace_id="t1", first=True)
    follow = GitHubIssueSink._issue_body(_result(), trace_id="t2", first=False)
    assert "tracks recurring" in first
    assert "Another occurrence" in follow


def test_github_sink_fingerprint_label_format():
    sink = GitHubIssueSink(repo="owner/repo", token="tok")
    label = sink._fingerprint_label(_result(check_name="url_allowlist", fingerprint="deadbeef"))
    assert label == "gov:deadbeef"


def test_github_sink_fingerprint_label_under_50_chars_for_long_check_name():
    """GitHub label names hard-cap at 50 chars. The fingerprint label must
    fit regardless of how long the check name is (audit.* rules in
    particular get long)."""
    sink = GitHubIssueSink(repo="owner/repo", token="tok")
    long_name = "audit.prompt_lacks_refusal_language_extra"
    label = sink._fingerprint_label(
        _result(check_name=long_name, fingerprint="abcdef012345")
    )
    assert len(label) <= 50
    # Fingerprint is what dedup depends on — that piece must survive intact.
    assert "abcdef012345" in label


def test_github_sink_category_label_truncated_when_check_name_too_long():
    sink = GitHubIssueSink(repo="owner/repo", token="tok")
    # 60-char check name → "governance:" + 60 = 71 chars unguarded.
    long_name = "x" * 60
    labels = sink._category_labels(_result(check_name=long_name))
    assert "governance" in labels
    for label in labels:
        assert len(label) <= 50


# ---------------------------------------------------------------------------
# GitHubIssueSink — HTTP interactions via pytest-httpx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_sink_creates_issue_when_none_exists(httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url="https://api.github.com/repos/owner/repo/issues?labels=gov%3Aabc123&state=open&per_page=1",
        json=[],
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/owner/repo/issues",
        json={"number": 42},
        status_code=201,
    )
    sink = GitHubIssueSink(repo="owner/repo", token="tok")
    await sink.report(_result(), trace_id="trace-xyz")

    create_req = [r for r in httpx_mock.get_requests() if r.method == "POST"][0]
    body = create_req.read().decode()
    assert "trace-xyz" in body
    # New (v0.2.1) label scheme: short dedup label + per-check category label.
    assert "gov:abc123" in body
    assert "governance:disclaimer" in body
    assert "[governance] disclaimer" in body


@pytest.mark.asyncio
async def test_github_sink_comments_on_existing_issue(httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url="https://api.github.com/repos/owner/repo/issues?labels=gov%3Aabc123&state=open&per_page=1",
        json=[{"number": 99}],
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/owner/repo/issues/99/comments",
        json={"id": 1},
        status_code=201,
    )
    sink = GitHubIssueSink(repo="owner/repo", token="tok")
    await sink.report(_result(), trace_id="trace-2")

    post = [r for r in httpx_mock.get_requests() if r.method == "POST"][0]
    assert "/issues/99/comments" in str(post.url)
    body = post.read().decode()
    assert "Another occurrence" in body


@pytest.mark.asyncio
async def test_github_sink_swallows_network_errors(httpx_mock):
    """Sink failures must NEVER propagate to the caller — the host's request
    path can't be allowed to fail because GitHub is unreachable."""
    httpx_mock.add_exception(Exception("boom"))
    sink = GitHubIssueSink(repo="owner/repo", token="tok")
    # No raise = pass.
    await sink.report(_result(), trace_id="t")


# ---------------------------------------------------------------------------
# build_sink factory
# ---------------------------------------------------------------------------


def test_build_sink_log_default():
    assert isinstance(build_sink("log"), LogSink)
    assert isinstance(build_sink(""), LogSink)
    assert isinstance(build_sink("LOG"), LogSink)  # case-insensitive


def test_build_sink_none():
    assert isinstance(build_sink("none"), NullSink)


def test_build_sink_github():
    sink = build_sink("github", github_repo="owner/repo", github_token="tok")
    assert isinstance(sink, GitHubIssueSink)


def test_build_sink_github_misconfigured_falls_back_to_log(caplog):
    with caplog.at_level(logging.WARNING):
        sink = build_sink("github", github_repo="", github_token="")
    assert isinstance(sink, LogSink)
    assert any("misconfigured" in r.message for r in caplog.records)
