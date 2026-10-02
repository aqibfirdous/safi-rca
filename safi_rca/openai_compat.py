"""Compatibility shim: let Aider's RepoMap work with openai >= 1.0.

Aider 0.16 reads the module-level ``openai.api_base`` at import time
(``aider/models/model.py``), and again when deciding which chat backend to use
(``aider/sendchat.py``).  Both are the substring test
``if "openrouter.ai" in openai.api_base``.  The openai 1.x/2.x client moved that
setting onto the client object, so the attribute no longer exists at module level
and ``import aider.repomap`` raises ``AttributeError``.

Neither Aider nor the installed package is modified: the attribute is restored on
the module at import time.  The default is the public OpenAI endpoint, which
makes the substring test evaluate to ``False`` -- the correct answer for a
RepoMap run, because the RepoMap never calls a model.

``openhands-sdk`` requires openai >= 2.20, while aider-chat 0.16 pins
``openai==0.27.6``.  They cannot share a resolved dependency set, so on any
environment that hosts the agent runtime Aider has to run against the modern
client.  This shim is what lets both integrations coexist.
"""

from __future__ import annotations

_APPLIED = False

# The value aider 0.27 shipped as its default; keeps the openrouter.ai test false.
DEFAULT_API_BASE = "https://api.openai.com/v1"


def install() -> bool:
    """Restore ``openai.api_base`` when the installed openai client lacks it."""
    global _APPLIED
    if _APPLIED:
        return True
    try:
        import openai  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - openai is optional
        return False

    if not hasattr(openai, "api_base"):
        openai.api_base = DEFAULT_API_BASE  # type: ignore[attr-defined]
    if not hasattr(openai, "api_type"):
        openai.api_type = "open_ai"  # type: ignore[attr-defined]
    _APPLIED = True
    return True