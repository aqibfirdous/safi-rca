"""OpenHands agent runtime.

OpenHands is the agent runtime for this product: safi-rca assembles the context
(repo training + role training + RepoMap + source at the exact sha + failure
evidence) and hands it to an OpenHands agent running against a read-only
workspace.

The adapter is deliberately thin and uses only documented SDK entry points:

* ``OpenHandsAgentSettings(...).create_agent()`` -- the SDK's documented factory
  ("single defaulting point" for tool selection),
* ``LocalWorkspace(working_dir=...)`` rooted at the immutable ``git archive``
  export of the analysed commit,
* ``LocalConversation(agent=..., workspace=...)`` with ``send_message`` /
  ``run``.

Read-only is enforced *structurally*, not only by instruction: the agent is
granted exactly two tools, ``ThinkTool`` and ``FinishTool``.  Neither writes a
file, and the SDK ships no shell or file-write tool, so the agent has no way to
mutate the tree even if it tried.  Everything it needs to reason about is
already in the prompt, which is why no browsing tools are required.

If OpenHands or model credentials are unavailable, :meth:`availability` returns
``False`` with the reason and the caller falls back to the local runtime.  When
``--runtime openhands`` is requested explicitly, the unavailability is raised
instead of silently downgraded.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..context import AnalysisContext
from ..errors import RuntimeUnavailable
from ..report import RcaReport, report_from_dict
from .base import RuntimeResult

READ_ONLY_INSTRUCTION = (
    "You are diagnosing a failure. You must NOT create, edit, delete, move or "
    "format any file, must not run commands that change state, and must not "
    "commit. The workspace is an immutable export of commit {sha}; treat every "
    "file as read-only."
)

#: The only tools granted to the RCA agent.  Both are pure reasoning/termination
#: tools -- the SDK provides no shell or file-write tool, so nothing here can
#: modify the analysed tree.
READ_ONLY_TOOLS = ("ThinkTool", "FinishTool")

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

LLM_ENV_KEYS = ("LLM_API_KEY", "SAFI_RCA_LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")

DEFAULT_MODEL = "anthropic/claude-sonnet-4-5"


@dataclass
class _Backend:
    """Resolved SDK entry points."""

    label: str
    sdk: object
    llm_cls: object
    settings_cls: object
    tool_cls: object

    def is_legacy_core(self) -> bool:
        """True for the classic ``openhands.core`` layout, which takes a model."""
        return self.llm_cls is None


def _import_optional(name: str):
    try:
        return __import__(name, fromlist=["_"])
    except Exception as exc:  # noqa: BLE001
        raise RuntimeUnavailable(f"cannot import {name}: {type(exc).__name__}: {exc}") from exc


def _resolve_backend() -> _Backend:
    """Find the OpenHands agent entry points, without depending on internals."""
    errors: list[str] = []

    # The SDK prints an ASCII banner on import.  It goes to stdout and would
    # corrupt `safi-rca --json` and the `runtimes` report, so it is suppressed
    # before the first import.
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

    try:
        sdk = _import_optional("openhands.sdk")
        llm_mod = _import_optional("openhands.sdk.llm")
        settings_mod = _import_optional("openhands.sdk.settings.model")
        tool_mod = _import_optional("openhands.sdk.tool")

        llm_cls = getattr(llm_mod, "LLM", None)
        settings_cls = getattr(settings_mod, "OpenHandsAgentSettings", None)
        tool_cls = getattr(tool_mod, "Tool", None)
        missing = [
            name
            for name, value in (
                ("LLM", llm_cls),
                ("OpenHandsAgentSettings", settings_cls),
                ("Tool", tool_cls),
            )
            if value is None
        ]
        if not missing and hasattr(sdk, "LocalConversation"):
            return _Backend("openhands.sdk", sdk, llm_cls, settings_cls, tool_cls)
        errors.append("openhands.sdk is missing: " + (", ".join(missing) or "LocalConversation"))
    except RuntimeUnavailable as exc:
        errors.append(str(exc))

    try:
        core = _import_optional("openhands.core")
        agent_cls = getattr(core, "Agent", None)
        if agent_cls is not None:
            # Classic core layout: an Agent that takes the model directly.
            return _Backend("openhands.core", None, None, None, None)
        errors.append("openhands.core exposes no Agent class")
    except RuntimeUnavailable as exc:
        errors.append(str(exc))

    raise RuntimeUnavailable("; ".join(errors) or "no OpenHands agent entry point found")


def _credentials() -> tuple[str, str]:
    for key in LLM_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            return key, value
    return "", ""


def build_read_only_agent(backend: _Backend, model: str, api_key: str):
    """Construct an OpenHands agent that has no way to write anything.

    ``tools`` is an explicit list rather than ``None``: the SDK treats ``None``
    as "the canonical default set", and we want the narrower, explicitly bare
    agent.  ``FinishTool`` is the agent's own answer channel and ``ThinkTool``
    lets it reason before committing to that answer.
    """

    tools = [backend.tool_cls(name=name) for name in READ_ONLY_TOOLS]
    settings = backend.settings_cls(
        llm=backend.llm_cls(model=model, api_key=api_key),
        tools=tools,
        enable_switch_llm_tool=False,
    )
    return settings.create_agent()


def run_conversation(backend: _Backend, agent, workspace_dir: Path, prompt: str) -> str:
    """Run one read-only conversation and return the agent's final text."""

    workspace = backend.sdk.LocalWorkspace(working_dir=str(workspace_dir))
    conversation = backend.sdk.LocalConversation(
        agent=agent,
        workspace=workspace,
        delete_on_close=False,
    )
    try:
        conversation.send_message(prompt)
        conversation.run()
        return _final_agent_text(conversation)
    finally:
        close = getattr(conversation, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - teardown must not mask the result
                pass


def _final_agent_text(conversation) -> str:
    """Return the text of the last assistant message in the event log."""

    from openhands.sdk.event import MessageEvent  # noqa: PLC0415
    from openhands.sdk.llm import content_to_str  # noqa: PLC0415

    try:
        events = list(conversation.state.events)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeUnavailable(f"cannot read OpenHands conversation state: {exc}") from exc

    text = ""
    for event in events:
        if not isinstance(event, MessageEvent) or getattr(event, "source", None) != "agent":
            continue
        chunks = getattr(event, "extended_content", None)
        rendered = content_to_str(chunks) if chunks else ""
        if not rendered.strip():
            rendered = content_to_str(getattr(event.llm_message, "content", None) or [])
        if rendered.strip():
            text = rendered
    if not text.strip():
        raise RuntimeUnavailable("OpenHands conversation produced no assistant message")
    return text


class OpenHandsRuntime:
    name = "openhands"

    def __init__(self, model: str | None = None, read_only: bool = True):
        self.model = model or os.environ.get("SAFI_RCA_LLM_MODEL") or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        self.read_only = read_only

    # ---------------------------------------------------------------- wiring
    def availability(self) -> tuple[bool, str]:
        try:
            backend = _resolve_backend()
        except RuntimeUnavailable as exc:
            return False, str(exc)
        key, _value = _credentials()
        if not key:
            return False, f"openhands present ({backend.label}) but no model credentials; set one of {LLM_ENV_KEYS}"
        return True, f"openhands {backend.label} with credentials from {key}, read-only tools {READ_ONLY_TOOLS}"

    # ------------------------------------------------------------------- run
    def run(self, ctx: AnalysisContext) -> RuntimeResult:
        usable, reason = self.availability()
        if not usable:
            raise RuntimeUnavailable(f"OpenHands runtime requested but unusable: {reason}")

        backend = _resolve_backend()
        key, _value = _credentials()
        prompt = ctx.render_prompt() + "\n\n" + READ_ONLY_INSTRUCTION.format(sha=ctx.sha)

        if backend.is_legacy_core():  # pragma: no cover - classic core layout
            reply = _run_core_agent(backend, prompt, self.model, key)
            label = backend.label
        else:
            agent = build_read_only_agent(backend, self.model, os.environ.get(key, ""))
            reply = run_conversation(backend, agent, ctx.export_root, prompt)
            label = f"{backend.label} (read-only, tools={list(READ_ONLY_TOOLS)})"

        report = _parse_report(reply, ctx)
        return RuntimeResult(
            runtime=self.name,
            report=report,
            notes=[reason, "agent ran read-only against an immutable export of the analysed commit"],
            details={"backend": label, "model": self.model, "read_only": self.read_only},
        )


def _run_core_agent(backend: _Backend, prompt: str, model: str, key: str) -> str:  # pragma: no cover - version dependent
    os.environ.setdefault("LLM_MODEL", model)
    if key:
        os.environ.setdefault("LLM_API_KEY", os.environ.get(key, ""))
    agent_cls = backend.sdk
    try:
        agent = agent_cls(llm=model, tools=[])
    except TypeError:
        agent = agent_cls(llm=model)
    result = agent.run(prompt)
    return getattr(result, "content", str(result))


def _parse_report(reply: str, ctx: AnalysisContext) -> RcaReport:
    match = _JSON_BLOCK.search(reply or "")
    if not match:
        raise RuntimeUnavailable("OpenHands reply contained no JSON report block")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise RuntimeUnavailable(f"OpenHands reply was not valid JSON: {exc}") from exc

    report = report_from_dict(data)
    # The report always refers to the sha that was analysed, never to a branch.
    report.repo = ctx.repo_name
    report.sha = ctx.sha
    report.sha_short = ctx.sha[:9]
    report.role = "root-cause-analyzer"
    report.commit = ctx.commit
    report.repamap = ctx.repomap.as_dict()
    report.training = ctx.training.as_dict()
    if not report.symptom:
        report.symptom = ctx.evidence.exception or "failure reported in the supplied evidence"
    return report