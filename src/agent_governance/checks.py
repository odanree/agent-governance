"""Built-in governance checks.

A `Check` is anything with a `name` attribute and an async `run(state) -> CheckResult`.
Hosts can also implement their own — the Protocol below is the only contract.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable


Severity = Literal["info", "warning", "violation"]


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single check run.

    `mutated_answer=True` means the node should adopt `new_answer` as the
    new value of `state["answer"]` before running subsequent checks.
    """

    check_name: str
    fired: bool
    severity: Severity
    detail: str  # short, safe-to-log; never include raw user query or full answer
    fingerprint: str  # stable hash for incident deduplication
    mutated_answer: bool = False
    new_answer: str | None = None


@runtime_checkable
class Check(Protocol):
    name: str

    async def run(self, state: dict) -> CheckResult: ...


def fingerprint(*parts: str) -> str:
    """Stable short hash for dedup labels — same inputs always produce the
    same fingerprint across processes and platforms.
    """
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return h.hexdigest()[:12]


# ---------------------------------------------------------------------------
# DisclaimerCheck — enforce an italic provenance disclaimer.
#
# Use case: the agent's answer must end with an italic note (e.g. "*Owner data
# is synthetic*") when the retrieved facts come from a non-authoritative
# source. The prompt instructs the LLM to do this; this check is the
# structural backstop.
# ---------------------------------------------------------------------------


# Default detector: any italic span (* or _) on one line that mentions
# synthetic/illustrative/non-authoritative provenance. Override via `pattern`.
DEFAULT_DISCLAIMER_PATTERN = re.compile(
    r"[*_][^*_\n]*(synthetic|illustrative|not\s+from\s+authoritative|paywalled)[^*_\n]*[*_]",
    re.IGNORECASE,
)


@dataclass
class DisclaimerCheck:
    """Enforce that the answer carries a provenance disclaimer when the
    upstream state has flagged retrieved data as non-authoritative.

    Configuration:
      canonical: the exact line to append when the model dropped its own.
      pattern:   regex that determines "the model already included a disclaimer."
      state_key: where to look for the disclaimer requirement in state.
                 The check fires when `state[state_key]["disclaimer"]` is truthy
                 AND the answer does not match `pattern`.
    """

    canonical: str
    pattern: re.Pattern = field(default=DEFAULT_DISCLAIMER_PATTERN)
    state_key: str = "provenance"
    name: str = "disclaimer"

    async def run(self, state: dict) -> CheckResult:
        provenance = state.get(self.state_key) or {}
        expected = provenance.get("disclaimer") if isinstance(provenance, dict) else None
        answer = state.get("answer") or ""

        if not expected:
            return CheckResult(
                check_name=self.name,
                fired=False,
                severity="info",
                detail=f"no disclaimer required (state.{self.state_key}.disclaimer is empty)",
                fingerprint=fingerprint(self.name, "no-op"),
            )

        if self.pattern.search(answer):
            return CheckResult(
                check_name=self.name,
                fired=False,
                severity="info",
                detail="model included its own italic provenance note",
                fingerprint=fingerprint(self.name, "passed"),
            )

        new_answer = answer.rstrip() + "\n\n" + self.canonical
        return CheckResult(
            check_name=self.name,
            fired=True,
            severity="warning",
            detail="model dropped disclaimer; canonical appended",
            fingerprint=fingerprint(self.name, "drop"),
            mutated_answer=True,
            new_answer=new_answer,
        )


# ---------------------------------------------------------------------------
# URLAllowlistCheck — redact non-allowlisted URLs from the answer.
# ---------------------------------------------------------------------------


# Conservative: matches http(s) URLs and bare TLD-shaped hostnames the model
# might hallucinate (e.g. "ocassessor.gov"). Tune per project if needed.
DEFAULT_URL_PATTERN = re.compile(
    r"(https?://[^\s)\]]+|\b[a-z0-9-]+\.(?:gov|com|org|net|io)\b(?:/[^\s)\]]*)?)",
    re.IGNORECASE,
)


@dataclass
class URLAllowlistCheck:
    """Redact URLs in the answer that aren't on the allowlist.

    `allowlist`: hostnames (or suffix-matched parent domains). Default empty
    means RULE-4 style "no URLs at all" enforcement.
    """

    allowlist: list[str] = field(default_factory=list)
    pattern: re.Pattern = field(default=DEFAULT_URL_PATTERN)
    redaction_text: str = "[URL removed by governance: not on allowlist]"
    name: str = "url_allowlist"

    def __post_init__(self) -> None:
        self._allowlist = {a.lower().strip() for a in self.allowlist if a.strip()}

    async def run(self, state: dict) -> CheckResult:
        answer = state.get("answer") or ""
        matches = self.pattern.findall(answer)
        if not matches:
            return CheckResult(
                check_name=self.name,
                fired=False,
                severity="info",
                detail="no URLs in answer",
                fingerprint=fingerprint(self.name, "clean"),
            )

        redacted_urls = [m for m in matches if not self._allowed(m)]
        if not redacted_urls:
            return CheckResult(
                check_name=self.name,
                fired=False,
                severity="info",
                detail=f"{len(matches)} URL(s) found, all allowlisted",
                fingerprint=fingerprint(self.name, "allowed"),
            )

        new_answer = answer
        for url in redacted_urls:
            new_answer = new_answer.replace(url, self.redaction_text)

        hosts = sorted({self._host(u) for u in redacted_urls})
        return CheckResult(
            check_name=self.name,
            fired=True,
            severity="violation",
            detail=f"redacted {len(redacted_urls)} non-allowlisted URL(s): " + ", ".join(hosts),
            # Same offending host produces the same fingerprint so recurring
            # hallucinations dedupe instead of opening a new issue each time.
            fingerprint=fingerprint(self.name, *hosts),
            mutated_answer=True,
            new_answer=new_answer,
        )

    def _allowed(self, url: str) -> bool:
        host = self._host(url)
        return any(host == a or host.endswith("." + a) for a in self._allowlist)

    @staticmethod
    def _host(url: str) -> str:
        s = url.lower()
        s = re.sub(r"^https?://", "", s)
        s = s.split("/", 1)[0]
        return s


# ---------------------------------------------------------------------------
# PromptInjectionCheck — read-only observability for injection attempts.
# ---------------------------------------------------------------------------


DEFAULT_INJECTION_PATTERNS = (
    r"ignore\s+(?:all\s+|the\s+)?(?:previous|prior|above)\s+instructions",
    r"disregard\s+(?:all\s+|the\s+)?(?:previous|prior|above)",
    r"\byou\s+are\s+now\b",
    r"\bfrom\s+now\s+on\s+you\b",
    r"\bsystem\s*:",
    r"<\|im_start\|>",
    r"\bnew\s+instructions\b",
)


@dataclass
class PromptInjectionCheck:
    """Scan `state["query"]` for known prompt-injection patterns.

    Read-only: never mutates the answer. Reports `info` severity so a sink
    captures it for later analysis without treating every match as a blocker.
    """

    patterns: tuple[str, ...] = field(default=DEFAULT_INJECTION_PATTERNS)
    state_key: str = "query"
    name: str = "prompt_injection"

    def __post_init__(self) -> None:
        self._compiled = [re.compile(p, re.IGNORECASE) for p in self.patterns]

    async def run(self, state: dict) -> CheckResult:
        text = state.get(self.state_key) or ""
        hits = [p.pattern for p in self._compiled if p.search(text)]
        if not hits:
            return CheckResult(
                check_name=self.name,
                fired=False,
                severity="info",
                detail="no injection patterns matched",
                fingerprint=fingerprint(self.name, "clean"),
            )
        return CheckResult(
            check_name=self.name,
            fired=True,
            severity="info",
            detail=f"matched {len(hits)} injection pattern(s)",
            fingerprint=fingerprint(self.name, *sorted(hits)),
        )
