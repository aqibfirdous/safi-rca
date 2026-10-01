"""Agent runtime slot.

The runtime is the thing that turns an assembled context into a report.  It is
pluggable so that OpenHands can be the production runtime while the acceptance
suite stays deterministic and offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..context import AnalysisContext
from ..report import RcaReport


@dataclass
class RuntimeResult:
    runtime: str
    report: RcaReport
    notes: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


class AgentRuntime(Protocol):
    name: str

    def availability(self) -> tuple[bool, str]:
        """Return ``(usable, reason)``."""

    def run(self, ctx: AnalysisContext) -> RuntimeResult:
        """Produce a report for ``ctx``."""


__all__ = ["AgentRuntime", "RuntimeResult"]
