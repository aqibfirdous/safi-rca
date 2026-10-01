"""Deterministic, offline RCA runtime.

Used when the OpenHands runtime is not usable in this environment (no OpenHands
install, or no model credentials).  It runs the same assembled context through
the same report contract, so the acceptance suite is reproducible without a
network call and without a model.

It is read-only by construction: it reads an exported copy of one commit and
writes nothing but the returned report.
"""

from __future__ import annotations

from ..analyst import diagnose
from ..context import AnalysisContext
from .base import RuntimeResult


class LocalDeterministicRuntime:
    name = "local-deterministic"

    def availability(self) -> tuple[bool, str]:
        return True, "always available"

    def run(self, ctx: AnalysisContext) -> RuntimeResult:
        report = diagnose(ctx)
        return RuntimeResult(
            runtime=self.name,
            report=report,
            notes=[
                "reasoning performed offline by the built-in evidence-driven engine",
                "no model was called; every statement is derived from the evidence and the exported commit",
            ],
            details={"mode": "deterministic", "model": None},
        )
