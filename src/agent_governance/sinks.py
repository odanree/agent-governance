"""Incident sinks — where fired checks get reported.

`LogSink` is the default; safe for dev/CI. `GitHubIssueSink` is opt-in,
deduplicates by fingerprint label, and only carries safe metadata in the
issue body — never the raw user query or full answer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Protocol

import httpx

from agent_governance.checks import CheckResult

log = logging.getLogger(__name__)


class IncidentSink(Protocol):
    async def report(self, result: CheckResult, trace_id: str | None) -> None: ...


class LogSink:
    """Default — structured log line. Zero external side effects."""

    async def report(self, result: CheckResult, trace_id: str | None) -> None:
        log.warning(
            "governance check=%s severity=%s detail=%r fingerprint=%s trace_id=%s",
            result.check_name,
            result.severity,
            result.detail,
            result.fingerprint,
            trace_id,
        )


class NullSink:
    """No-op. Use in tests."""

    async def report(self, result: CheckResult, trace_id: str | None) -> None:
        return None


class GitHubIssueSink:
    """Files (or comments on) a GitHub issue per unique fingerprint.

    Dedup: each `CheckResult.fingerprint` becomes a label
    `governance:<check>:<fingerprint>`. The sink first searches open issues
    with that label. If one exists, it adds a comment with the new trace_id;
    otherwise it opens a new issue.

    Privacy: issue bodies carry only safe metadata — check name, detail,
    trace_id, timestamp. The user query and full answer are never posted.
    """

    def __init__(
        self,
        repo: str,
        token: str,
        base_url: str = "https://api.github.com",
        timeout: float = 10.0,
        extra_labels: list[str] | None = None,
    ) -> None:
        if not repo or "/" not in repo:
            raise ValueError(
                "GitHubIssueSink requires repo in 'owner/repo' form (got %r)" % repo
            )
        if not token:
            raise ValueError("GitHubIssueSink requires a token")
        self.repo = repo
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.extra_labels = list(extra_labels or [])

    async def report(self, result: CheckResult, trace_id: str | None) -> None:
        label = self._fingerprint_label(result)
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "agent-governance",
                },
                timeout=self.timeout,
            ) as client:
                existing = await self._find_open_issue(client, label)
                if existing is None:
                    await self._create_issue(client, result, trace_id, label)
                else:
                    await self._append_comment(client, existing, result, trace_id)
        except Exception as e:  # pragma: no cover - networking edge
            # Sink failures MUST NEVER block the request — log and move on.
            log.exception("GitHubIssueSink failed: %s", e)

    def _fingerprint_label(self, result: CheckResult) -> str:
        return f"governance:{result.check_name}:{result.fingerprint}"

    async def _find_open_issue(self, client: httpx.AsyncClient, label: str) -> int | None:
        r = await client.get(
            f"/repos/{self.repo}/issues",
            params={"labels": label, "state": "open", "per_page": 1},
        )
        r.raise_for_status()
        data = r.json()
        return data[0]["number"] if data else None

    async def _create_issue(
        self,
        client: httpx.AsyncClient,
        result: CheckResult,
        trace_id: str | None,
        label: str,
    ) -> None:
        title = f"[governance] {result.check_name}: {result.detail[:120]}"
        body = self._issue_body(result, trace_id, first=True)
        labels = [
            "governance",
            f"governance:{result.check_name}",
            label,
            *self.extra_labels,
        ]
        r = await client.post(
            f"/repos/{self.repo}/issues",
            json={"title": title, "body": body, "labels": labels},
        )
        r.raise_for_status()
        log.info(
            "governance: opened issue #%s for fingerprint=%s",
            r.json()["number"],
            result.fingerprint,
        )

    async def _append_comment(
        self,
        client: httpx.AsyncClient,
        issue_number: int,
        result: CheckResult,
        trace_id: str | None,
    ) -> None:
        body = self._issue_body(result, trace_id, first=False)
        r = await client.post(
            f"/repos/{self.repo}/issues/{issue_number}/comments",
            json={"body": body},
        )
        r.raise_for_status()
        log.info(
            "governance: commented on issue #%s for fingerprint=%s",
            issue_number,
            result.fingerprint,
        )

    @staticmethod
    def _issue_body(result: CheckResult, trace_id: str | None, first: bool) -> str:
        prefix = (
            "Governance check fired. This issue tracks recurring occurrences "
            "of this specific fingerprint."
            if first
            else "Another occurrence of this fingerprint."
        )
        trace_line = (
            f"- **Trace:** `{trace_id}` (full query + answer reachable in the tracing backend under auth)"
            if trace_id
            else "- **Trace:** _none — tracing was disabled for this request_"
        )
        return "\n".join(
            [
                prefix,
                "",
                f"- **Check:** `{result.check_name}`",
                f"- **Severity:** `{result.severity}`",
                f"- **Detail:** {result.detail}",
                f"- **Fingerprint:** `{result.fingerprint}`",
                trace_line,
                f"- **Timestamp:** {datetime.now(timezone.utc).isoformat()}",
                "",
                "_Filed automatically by `agent-governance`._",
            ]
        )


# ---------------------------------------------------------------------------
# Factory — used by hosts that want config-driven sink selection.
# ---------------------------------------------------------------------------


def build_sink(
    kind: str,
    *,
    github_repo: str = "",
    github_token: str = "",
    extra_labels: list[str] | None = None,
) -> IncidentSink:
    """Construct an IncidentSink from a string identifier.

    Falls back to LogSink on any misconfiguration so dev/CI never breaks.
    Valid kinds: "log" (default), "github", "none".
    """
    kind = (kind or "log").strip().lower()
    if kind == "none":
        return NullSink()
    if kind == "github":
        try:
            return GitHubIssueSink(
                repo=github_repo,
                token=github_token,
                extra_labels=extra_labels,
            )
        except ValueError as e:
            log.warning(
                "agent-governance: GitHubIssueSink misconfigured (%s); falling back to LogSink",
                e,
            )
            return LogSink()
    return LogSink()
