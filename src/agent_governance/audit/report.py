"""Report formatters — Markdown for humans, JSON for machines."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone

from agent_governance.audit.rules import Finding
from agent_governance.audit.scanner import Inventory


_SEVERITY_ORDER = {"violation": 0, "warning": 1, "info": 2}


def format_markdown(inv: Inventory, findings: list[Finding]) -> str:
    """Human-readable Markdown report. Suitable for pasting into a PR or issue."""
    sev_counts = Counter(f.severity for f in findings)
    findings_sorted = sorted(findings, key=lambda f: (_SEVERITY_ORDER[f.severity], f.rule_id, f.location()))

    lines: list[str] = []
    lines.append(f"# agent-governance audit — `{inv.root.name}`")
    lines.append("")
    lines.append(f"_{datetime.now(timezone.utc).isoformat(timespec='seconds')}_  ·  scanned `./{inv.root.name}`")
    lines.append("")

    # Inventory summary
    lines.append("## Inventory")
    lines.append("")
    lines.append(f"- **Python files scanned:** {inv.file_count}")
    lines.append(f"- **LLM call sites:** {len(inv.llm_calls)}"
                 + (f" ({_sdk_summary(inv)})" if inv.llm_calls else ""))
    lines.append(f"- **Prompt locations:** {len(inv.prompts)}")
    lines.append(f"- **Eval setup:** {_yesno(bool(inv.eval_paths))}"
                 + (f"  ({', '.join(inv.eval_paths[:3])}{'…' if len(inv.eval_paths) > 3 else ''})" if inv.eval_paths else ""))
    lines.append(f"- **Tracing wired:** {_yesno(bool(inv.trace_providers))}"
                 + (f"  ({', '.join(sorted(inv.trace_providers))})" if inv.trace_providers else ""))
    lines.append(f"- **LangGraph used:** {_yesno(inv.uses_langgraph)}")
    lines.append(f"- **`agent-governance` wired:** {_yesno(inv.governance_node_wired)}")
    lines.append(f"- **`.env.example` present:** {_yesno(inv.env_example_present)}")
    lines.append("")

    # Findings summary
    lines.append("## Findings")
    lines.append("")
    if not findings:
        lines.append("_No findings — repo passes the v0.2 rule set. 🎉_")
        lines.append("")
        return "\n".join(lines)

    lines.append(
        f"**{len(findings)}** total"
        + f" · {sev_counts.get('violation', 0)} violation"
        + f" · {sev_counts.get('warning', 0)} warning"
        + f" · {sev_counts.get('info', 0)} info"
    )
    lines.append("")

    # Detail blocks
    for f in findings_sorted:
        badge = {"violation": "🚨 VIOLATION", "warning": "⚠️  WARNING", "info": "ℹ️  INFO"}[f.severity]
        lines.append(f"### {badge} · `{f.rule_id}`")
        lines.append(f"**{f.title}**")
        lines.append(f"_Location: `{f.location()}`_")
        lines.append("")
        lines.append(f.detail)
        lines.append("")
        lines.append(f"> **Recommendation:** {f.recommendation}")
        lines.append("")

    return "\n".join(lines)


def format_json(inv: Inventory, findings: list[Finding]) -> str:
    """Machine-readable JSON. For consuming in CI / dashboards."""
    payload = {
        "tool": "agent-governance audit",
        "version": _agent_governance_version(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scanned_path": str(inv.root),
        "inventory": {
            "python_files": inv.file_count,
            "llm_call_sites": [asdict(c) for c in inv.llm_calls],
            "prompt_sites": [asdict(p) for p in inv.prompts],
            "eval_paths": inv.eval_paths,
            "trace_providers": sorted(inv.trace_providers),
            "uses_langgraph": inv.uses_langgraph,
            "governance_node_wired": inv.governance_node_wired,
            "env_example_present": inv.env_example_present,
            "has_url_allowlist_check": inv.has_url_allowlist_check,
        },
        "findings": [
            {
                "rule_id": f.rule_id,
                "severity": f.severity,
                "title": f.title,
                "detail": f.detail,
                "recommendation": f.recommendation,
                "location": f.location(),
                "file": f.file,
                "line": f.line,
            }
            for f in findings
        ],
    }
    return json.dumps(payload, indent=2)


def _sdk_summary(inv: Inventory) -> str:
    return ", ".join(sorted({c.sdk for c in inv.llm_calls}))


def _yesno(b: bool) -> str:
    return "✅ yes" if b else "❌ no"


def _agent_governance_version() -> str:
    try:
        from agent_governance import __version__
        return __version__
    except Exception:
        return "unknown"
