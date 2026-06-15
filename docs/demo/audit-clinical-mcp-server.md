# agent-governance audit — `clinical-mcp-server`

_2026-06-15T07:52:06+00:00_  ·  scanned `./clinical-mcp-server`

## Inventory

- **Python files scanned:** 9
- **LLM call sites:** 0
- **Prompt locations:** 0
- **Eval setup:** ❌ no
- **Tracing wired:** ❌ no
- **LangGraph used:** ❌ no
- **`agent-governance` wired:** ❌ no
- **`.env.example` present:** ✅ yes

## Findings

**1** total · 0 violation · 1 warning · 0 info

### ⚠️  WARNING · `MISSING_URL_OUTPUT_VALIDATION`
**API responses surface URLs with no allowlist validation**
_Location: `server/tools/openfda.py:46`_

Response model in `server/tools/openfda.py:46` exposes a `url` field but no URLAllowlistCheck was detected in the repo. If the URL comes from LLM output (directly or via a tool that the LLM influenced), an hallucinated host can ship to users.

> **Recommendation:** Add `agent_governance.URLAllowlistCheck(allowlist=[…])` to a governance node, listing the hostnames you actually want to expose (your own domain, the upstream canonical sources). Anything else gets redacted.

