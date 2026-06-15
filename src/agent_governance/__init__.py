"""Governance node + incident reporting for LLM agents.

The package is framework-agnostic: the node function takes a `state` dict and
returns a `state` dict, so it composes with LangGraph, LangChain `Runnable`,
plain async pipelines, etc.

See the README for the integration guide and ADR-0001 for the design.
"""

from agent_governance.checks import (
    Check,
    CheckResult,
    DisclaimerCheck,
    PromptInjectionCheck,
    Severity,
    URLAllowlistCheck,
)
from agent_governance.node import build_governance_node
from agent_governance.observability import (
    NullObservabilityAdapter,
    ObservabilityAdapter,
)
from agent_governance.sinks import (
    GitHubIssueSink,
    IncidentSink,
    LogSink,
    NullSink,
    build_sink,
)

__version__ = "0.1.0"

__all__ = [
    "Check",
    "CheckResult",
    "DisclaimerCheck",
    "GitHubIssueSink",
    "IncidentSink",
    "LogSink",
    "NullObservabilityAdapter",
    "NullSink",
    "ObservabilityAdapter",
    "PromptInjectionCheck",
    "Severity",
    "URLAllowlistCheck",
    "__version__",
    "build_governance_node",
    "build_sink",
]
