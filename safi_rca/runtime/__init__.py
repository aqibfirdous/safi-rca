"""RCA runtimes.

``local``      -- deterministic, offline, evidence-driven engine (default fallback)
``openhands``  -- OpenHands agent runtime (production runtime)
``auto``       -- OpenHands when it is usable, otherwise ``local``
"""

from __future__ import annotations

from ..errors import InputError
from .base import AgentRuntime, RuntimeResult
from .local_runtime import LocalDeterministicRuntime
from .openhands_runtime import OpenHandsRuntime

RUNTIMES: dict[str, type] = {
    "local": LocalDeterministicRuntime,
    "openhands": OpenHandsRuntime,
}


def get_runtime(name: str) -> AgentRuntime:
    key = (name or "auto").lower()
    if key == "auto":
        hands = OpenHandsRuntime()
        usable, _reason = hands.availability()
        return hands if usable else LocalDeterministicRuntime()
    if key not in RUNTIMES:
        raise InputError(f"unknown runtime {name!r}; choose one of {sorted(RUNTIMES) + ['auto']}")
    return RUNTIMES[key]()


__all__ = ["AgentRuntime", "RuntimeResult", "RUNTIMES", "get_runtime"]
