"""Observability adapter — bridges the governance node to whatever tracing
system the host app uses (Langfuse, LangSmith, OpenTelemetry, custom).

The package never imports an observability vendor directly. Hosts supply an
adapter; tests use `NullObservabilityAdapter`.
"""

from __future__ import annotations

from typing import Protocol


class ObservabilityAdapter(Protocol):
    """Three hooks the governance node calls into.

    All three are no-op-safe — adapters that don't support a feature should
    silently return rather than raise. The node does not check return values.
    """

    def get_trace_id(self) -> str | None:
        """Return the current request's trace id, or None if tracing is off."""
        ...

    def tag_trace(self, tags: list[str], metadata: dict | None = None) -> None:
        """Attach tags/metadata to the active trace."""
        ...

    def create_score(self, trace_id: str, name: str, value: float, comment: str | None = None) -> None:
        """Attach a numeric score to a trace (for filtering in the dashboard)."""
        ...


class NullObservabilityAdapter:
    """Default adapter — no-ops everything. Use when no tracing is available."""

    def get_trace_id(self) -> str | None:
        return None

    def tag_trace(self, tags: list[str], metadata: dict | None = None) -> None:
        return None

    def create_score(self, trace_id: str, name: str, value: float, comment: str | None = None) -> None:
        return None
