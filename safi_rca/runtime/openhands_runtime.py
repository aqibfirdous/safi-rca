"""OpenHands agent runtime.

OpenHands is the agent runtime for this product: safi-rca assembles the context
(repo training + role training + RepoMap + source at the exact sha + failure
evidence) and hands it to an OpenHands agent running with a read-only workspace.

The adapter is deliberately thin.  It

* resolves the OpenHands entry points that expose a conversation/agent, across
  the SDK and the classic core layout,
* forces read-only operation (no write/execute tools are granted, and the agent
  is told the workspace is an export of one immutable commit),
* parses the model's JSON reply into the shared :class:`RcaReport` contract.

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

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

LLM_ENV_KEYS = ("LLM_API_KEY", "SAFI_RCA_LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")


@dataclass
class _Backend:
    label: str
    conversation_cls: type
    llm_factory: object
    tools: list = None


def _import_optional(name: str):
    try:
        return __import__(name, fromlist=["_"])
    except Exception as exc:  # noqa: BLE001
        raise RuntimeUnavailable(f"cannot import {name}: {type(exc).__name__}: {exc}") from exc


def _resolve_backend() -> _Backend:
    """Find an OpenHands agent entry point without depending on internals."""
    errors: list[str] = []

    try:
        sdk = _import_optional("openhands.sdk")
        conversation = getattr(sdk, "Conversation", None) or getattr(sdk, "LocalConversation", None)
        llm = getattr(sdk, "Llm", None) or getattr(sdk, "LLM", None)
        tools = getattr(sdk, "Tool", None)
        tool_list = []
        if tools is not None:
            for name in ("ReadOnlyBashTool", "ReadFileTool", "GrepTool", "GlobTool"):
                factory = getattr(tools, name, None)
                if factory is not None:
                    tool_list.append(factory)
        if conversation is not None and llm is not None:
            return _Backend("openhands.sdk", conversation, llm, tool_list)
        errors.append("openhands.sdk exposes no Conversation/Llm pair")
    except RuntimeUnavailable as exc:
        errors.append(str(exc))

    try:
        core = _import_optional("openhands.core")
        agent_cls = getattr(core, "Agent", None)
        if agent_cls is not None:
            return _Backend("openhands.core", agent_cls, None, [])
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


class OpenHandsRuntime:
    name = "openhands"

    def __init__(self, model: str | None = None, read_only: bool = True):
        self.model = model or os.environ.get("SAFI_RCA_LLM_MODEL") or os.environ.get("LLM_MODEL") or "anthropic/claude-sonnet-4-5"
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
        return True, f"openhands {backend.label} with credentials from {key}"

    # ------------------------------------------------------------------- run
    def run(self, ctx: AnalysisContext) -> RuntimeResult:
        usable, reason = self.availability()
        if not usable:
            raise RuntimeUnavailable(f"OpenHands runtime requested but unusable: {reason}")

        backend = _resolve_backend()
        key, _value = _credentials()
        prompt = ctx.render_prompt() + "\n\n" + READ_ONLY_INSTRUCTION.format(sha=ctx.sha)

        if backend.llm_factory is not None:  # SDK shape
            llm = backend.llm_factory(model=self.model, api_key=os.environ.get(key))
            conversation = backend.conversation_cls(llm=llm)
            for tool in backend.tools or []:
                conversation = _with_tool(conversation, tool)
            reply = _run_sdk_conversation(conversation, prompt)
        else:  # classic core shape
            reply = _run_core_agent(backend.conversation_cls, prompt, self.model, key)

        report = _parse_report(reply, ctx)
        return RuntimeResult(
            runtime=self.name,
            report=report,
            notes=[reason, "agent ran read-only against an immutable export of the analysed commit"],
            details={"backend": backend.label, "model": self.model, "read_only": self.read_only},
        )


def _with_tool(conversation, tool_factory):
    try:
        return conversation.add_tool(tool_factory())
    except Exception:  # noqa: BLE001 - tool wiring varies across versions
        return conversation


def _run_sdk_conversation(conversation, prompt: str) -> str:
    if hasattr(conversation, "send_message"):
        conversation.send_message(prompt)
    else:  # pragma: no cover - depends on installed version
        conversation.run(prompt)
    for attribute in ("last_message", "last_text", "text"):
        value = getattr(conversation, attribute, None)
        if isinstance(value, str) and value.strip():
            return value
        if value is not None and hasattr(value, "text"):
            return value.text
    raise RuntimeUnavailable("OpenHands conversation produced no assistant message")


def _run_core_agent(agent_cls, prompt: str, model: str, key: str) -> str:  # pragma: no cover - version dependent
    os.environ.setdefault("LLM_MODEL", model)
    if key:
        os.environ.setdefault("LLM_API_KEY", os.environ.get(key, ""))
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
