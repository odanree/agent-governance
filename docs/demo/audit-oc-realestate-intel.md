# agent-governance audit — `oc-realestate-intel`

_2026-06-15T07:52:06+00:00_  ·  scanned `./oc-realestate-intel`

## Inventory

- **Python files scanned:** 39
- **LLM call sites:** 7 (langchain_anthropic)
- **Prompt locations:** 4
- **Eval setup:** ✅ yes  (evals, evals/golden.yaml)
- **Tracing wired:** ✅ yes  (langfuse, opentelemetry)
- **LangGraph used:** ✅ yes
- **`agent-governance` wired:** ✅ yes
- **`.env.example` present:** ✅ yes

## Findings

**1** total · 0 violation · 0 warning · 1 info

### ℹ️  INFO · `PROMPT_LACKS_REFUSAL_LANGUAGE`
**Long prompt `ROUTER_SYSTEM` has no refusal / boundary language**
_Location: `app/agents/supervisor.py:44`_

The prompt at `app/agents/supervisor.py:44` is 1077 chars long but contains no occurrences of 'do not', 'never', 'must not', 'refuse', or similar boundary tokens. Long prompts with no negative constraints often underspecify what the model should refuse — leading to over-eager answers and hallucinated guidance.

> **Recommendation:** Add explicit refusal rules: 'If the facts do not contain the answer, say so', 'Never invent URLs / phone numbers / dosages', 'Do not characterize transfers as arm's-length or inter-family', etc. Use numbered RULES blocks so the eval can spot-check each one.

