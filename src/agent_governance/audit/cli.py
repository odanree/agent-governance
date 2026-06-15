"""`agent-governance audit` CLI.

Walks a Python repo, builds an inventory of LLM call sites + prompts +
evals + tracing, runs the rule set, and prints a Markdown or JSON report.
Optionally files findings as GitHub issues via the existing GitHubIssueSink.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Sequence

# Windows consoles default to cp1252, which chokes on the ✅ / ⚠️ etc emojis
# in the report. Force UTF-8 once at startup so stdout works everywhere.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from agent_governance import __version__
from agent_governance.audit.report import format_json, format_markdown
from agent_governance.audit.rules import Finding, run_rules
from agent_governance.audit.scanner import scan
from agent_governance.sinks import GitHubIssueSink

_SEVERITY_ORDER = {"violation": 0, "warning": 1, "info": 2}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-governance",
        description="Governance toolkit for LLM agents — runtime checks (package) + repo auditor (this CLI).",
    )
    parser.add_argument("--version", action="version", version=f"agent-governance {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    audit = sub.add_parser(
        "audit",
        help="Audit a Python repo for LLM governance gaps.",
    )
    audit.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Path to the repo root (default: current directory).",
    )
    audit.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format (default: markdown).",
    )
    audit.add_argument(
        "--fail-on",
        choices=("violation", "warning", "info"),
        default=None,
        help=(
            "Exit non-zero if any finding has at least this severity. "
            "Useful for CI: --fail-on=warning blocks merges on new warnings."
        ),
    )
    audit.add_argument(
        "--github",
        metavar="OWNER/REPO",
        help="File each finding as a GitHub issue in OWNER/REPO (deduplicated by rule + location).",
    )
    audit.add_argument(
        "--github-token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token (defaults to $GITHUB_TOKEN).",
    )
    audit.add_argument(
        "--output",
        "-o",
        metavar="PATH",
        help="Write the report to a file instead of stdout.",
    )

    args = parser.parse_args(argv)

    if args.cmd == "audit":
        return _do_audit(args)

    parser.print_help()  # pragma: no cover
    return 2


def _do_audit(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"agent-governance: not a directory: {root}", file=sys.stderr)
        return 2

    inv = scan(root)
    findings = run_rules(inv)

    # Emit the report.
    if args.format == "json":
        report = format_json(inv, findings)
    else:
        report = format_markdown(inv, findings)

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"agent-governance: wrote report to {args.output}", file=sys.stderr)
    else:
        print(report)

    # Optional: file as GitHub issues.
    if args.github:
        if not args.github_token:
            print(
                "agent-governance: --github requires --github-token (or $GITHUB_TOKEN)",
                file=sys.stderr,
            )
            return 2
        _dispatch_to_github(args.github, args.github_token, findings)

    # Exit code based on --fail-on.
    if args.fail_on:
        threshold = _SEVERITY_ORDER[args.fail_on]
        for f in findings:
            if _SEVERITY_ORDER[f.severity] <= threshold:
                return 1
    return 0


def _dispatch_to_github(repo: str, token: str, findings: list[Finding]) -> None:
    """File each finding as a GitHub issue via GitHubIssueSink.

    Dedup: each rule_id + location produces a stable fingerprint label, so
    re-running the audit on a repo whose gaps haven't changed just adds a
    comment to the existing issue rather than opening a new one."""
    sink = GitHubIssueSink(repo=repo, token=token, extra_labels=["audit"])

    async def _go() -> None:
        for f in findings:
            await sink.report(f.to_check_result(), trace_id=None)

    asyncio.run(_go())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
