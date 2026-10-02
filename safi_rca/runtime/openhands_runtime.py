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

#: The tool the agent calls to hand back its answer.
FINISH_TOOL = "FinishTool"

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

LLM_ENV_KEYS = (
    "LLM_API_KEY",
    "SAFI_RCA_LLM_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "LITELLM_API_KEY",
)

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

    # The SDK prints an ASCII banner to stderr on import.  safi-rca's stdout and
    # JSON output are a contract, so it is suppressed.  The check is normalised
    # because a value such as "1 " (which is what cmd.exe's `set X=1 & cmd`
    # produces) would otherwise fail the SDK's exact string comparison.
    if os.environ.get("OPENHANDS_SUPPRESS_BANNER", "").strip().lower() not in {"1", "true", "yes"}:
        os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"

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


#: Credential env var -> the model prefix litellm routes that key to.  A key from
#: one provider sent to another produces an opaque auth error deep inside
#: litellm, so the mismatch is reported up front instead.
KEY_PROVIDER_PREFIX = {
    "OPENROUTER_API_KEY": "openrouter/",
    "ANTHROPIC_API_KEY": "anthropic/",
    "OPENAI_API_KEY": None,  # OpenAI models carry no prefix
    "GEMINI_API_KEY": "gemini/",
}


def credential_mismatch(key: str, model: str) -> str | None:
    """Warn when a credential will be sent to a provider it is not for."""
    if key not in KEY_PROVIDER_PREFIX:
        return None
    expected = KEY_PROVIDER_PREFIX[key]
    if expected is None:
        return None if "/" not in model else f"{key} is an OpenAI key but {model!r} names another provider"
    if model.startswith(expected):
        return None
    return (
        f"{key} is a {expected.rstrip('/')} key but the model is {model!r}; "
        f"set LLM_MODEL={expected}<provider>/<model>, otherwise litellm sends the key "
        f"to the wrong provider and fails with an opaque auth error"
    )


def granted_tool_names(agent) -> set[str]:
    """Every tool the agent can actually call, explicit specs plus defaults."""
    explicit = {getattr(spec, "name", str(spec)) for spec in (agent.tools or [])}
    return explicit | set(getattr(agent, "include_default_tools", None) or [])


def build_read_only_agent(backend: _Backend, model: str, api_key: str):
    """Construct an OpenHands agent that has no way to write anything.

    ``tools=[]`` is deliberate and is the whole trick.  ``create_agent`` always
    injects ``BUILT_IN_TOOLS`` via ``include_default_tools``, and in SDK 1.50
    those are exactly ``FinishTool`` and ``ThinkTool``.  Naming them in ``tools``
    as well would load each one twice and the agent would refuse to start with
    ``Duplicate tool names found``.  So we ask for no tools of our own, switch
    the model-switching tool off, and then *verify* the resulting tool set is
    exactly the read-only allowlist -- so a future SDK that adds a shell or file
    -write tool to its defaults fails loudly instead of quietly granting write
    access to a repository under analysis.
    """

    settings = backend.settings_cls(
        llm=backend.llm_cls(model=model, api_key=api_key),
        tools=[],
        enable_switch_llm_tool=False,
    )
    agent = settings.create_agent()

    granted = granted_tool_names(agent)
    unexpected = granted - set(READ_ONLY_TOOLS)
    if unexpected:
        raise RuntimeUnavailable(
            f"refusing to run: the OpenHands agent would be granted non read-only "
            f"tools {sorted(unexpected)}; expected only {list(READ_ONLY_TOOLS)}"
        )
    missing = set(READ_ONLY_TOOLS) - granted
    if missing:
        raise RuntimeUnavailable(
            f"refusing to run: the OpenHands agent is missing expected tools {sorted(missing)}"
        )
    return agent


#: Sent when the agent reasoned well but ignored the output contract.  Observed
#: against a live model that had correctly spotted that the traceback could not
#: occur at the analysed sha, then answered in Markdown headings and so threw
#: away a good diagnosis on a formatting technicality.
RETRY_INSTRUCTION = (
    "Your analysis was not in the required format. Keep your conclusion exactly as it "
    "is, including its confidence and uncertainty, and restate it as ONE JSON object "
    "and nothing else. No prose, no Markdown headings, no code fence: reply with the "
    "bare JSON object starting with {{ and ending with }}. Keys: symptom, root_cause, "
    "affected_component, reasoning_summary, evidence (list of {kind, statement, file, "
    "line, symbol, quote}), confidence, uncertainty, recommended_next_action."
)


def run_conversation(backend: _Backend, agent, workspace_dir: Path, prompt: str) -> str:
    """Run one read-only conversation and return the agent's final text.

    If the reply cannot be read as the required JSON report, the agent is asked
    once more to restate the same analysis in the required shape.  That keeps a
    correct diagnosis that merely ignored the format, instead of reporting a
    runtime failure.
    """

    workspace = backend.sdk.LocalWorkspace(working_dir=str(workspace_dir))
    conversation = backend.sdk.LocalConversation(
        agent=agent,
        workspace=workspace,
        delete_on_close=False,
    )
    try:
        conversation.send_message(prompt)
        conversation.run()
        reply = _final_agent_text(conversation)
        if _looks_like_report(reply):
            return reply
        conversation.send_message(RETRY_INSTRUCTION)
        conversation.run()
        return _final_agent_text(conversation)
    finally:
        close = getattr(conversation, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - teardown must not mask the result
                pass


def _looks_like_report(reply: str) -> bool:
    """True when the reply already contains a parseable report object."""

    try:
        _extract_json_object(reply)
    except RuntimeUnavailable:
        return False
    return True


def _final_agent_text(conversation) -> str:
    """Return the agent's final answer from the conversation event log.

    The reply arrives in one of two shapes and which one appears is the model's
    choice, so both are handled:

    * a :class:`MessageEvent` whose ``source`` is ``agent``, with the text in
      ``extended_content`` or the underlying ``llm_message``; or
    * an :class:`ActionEvent` carrying a ``message`` -- the ``finish`` tool's
      answer channel, whose action deliberately renders an empty observation.

    The action form wins when both are present, since it is the terminal answer.
    """

    from openhands.sdk.event import MessageEvent  # noqa: PLC0415
    from openhands.sdk.llm import TextContent  # noqa: PLC0415

    try:
        events = list(conversation.state.events)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeUnavailable(f"cannot read OpenHands conversation state: {exc}") from exc

    action_text = ""
    message_text = ""
    for event in events:
        action = getattr(event, "action", None)
        if action is not None:
            message = getattr(action, "message", None)
            if isinstance(message, str) and message.strip():
                action_text = message
            continue

        if not isinstance(event, MessageEvent) or getattr(event, "source", None) != "agent":
            continue

        # `content_to_str` is a display helper: it returns a list and substitutes
        # `[Image: N URLs]` for non-text parts.  Reading the text parts directly
        # keeps an image placeholder out of the report we are about to parse.
        llm_message = getattr(event, "llm_message", None)
        parts = []
        for chunk in getattr(event, "extended_content", None) or getattr(
            llm_message, "content", None
        ) or []:
            if isinstance(chunk, TextContent):
                parts.append(chunk.text)
        rendered = "\n".join(parts)
        if not rendered.strip():
            rendered = getattr(llm_message, "reasoning_content", None) or ""
        if rendered.strip():
            message_text = rendered

    for candidate in (action_text, message_text):
        if candidate.strip():
            return candidate
    raise RuntimeUnavailable(
        "OpenHands conversation produced no assistant answer: no "
        f"{FINISH_TOOL} call and no agent message in {len(events)} events"
    )


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
        detail = f"openhands {backend.label} with credentials from {key}, model {self.model!r}, read-only tools {READ_ONLY_TOOLS}"
        warning = credential_mismatch(key, self.model)
        if warning:
            detail = f"{detail} -- WARNING: {warning}"
        return True, detail

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


def _extract_json_object(reply: str) -> dict:
    """Pull the report object out of a model reply.

    Three separate failure modes are handled, all of them observed against a
    live model:

    * the reply wraps the report in prose or a ```` ```json ```` fence, so a
      greedy ``\\{.*\\}`` can start on a brace that belongs to the prose;
    * the reply contains a *second* ``{...}`` after the report, which a greedy
      match swallows; and
    * the model writes a literal newline inside a JSON string, which is invalid
      strict JSON but is exactly what it means, so ``strict=False`` is used.
    """

    text = reply or ""
    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1)

    candidates = _balanced_objects(text)
    if not candidates:
        # Last resort: the original greedy match, in case of unusual nesting.
        greedy = _JSON_BLOCK.search(text)
        if greedy is None:
            raise RuntimeUnavailable("OpenHands reply contained no JSON report block")
        candidates = [greedy.group(0)]

    parsed: list[dict] = []
    first_error: json.JSONDecodeError | None = None
    for block in candidates:
        try:
            value = json.loads(block, strict=False)
        except json.JSONDecodeError as exc:
            first_error = first_error or exc
            continue
        if isinstance(value, dict):
            parsed.append(value)

    if not parsed:
        raise RuntimeUnavailable(f"OpenHands reply was not valid JSON: {first_error}")
    # Prose braces parse as nothing, so anything that reached here is a real
    # object.  Prefer one that looks like the report; otherwise take the biggest.
    for value in parsed:
        if "root_cause" in value:
            return value
    return max(parsed, key=len)


def _balanced_objects(text: str) -> list[str]:
    """Every brace-balanced ``{...}`` in order, ignoring braces inside strings."""

    found: list[str] = []
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        closed = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    found.append(text[start : index + 1])
                    closed = True
                    break
        if not closed:
            return found
        start = text.find("{", start + 1)
    return found


def _parse_report(reply: str, ctx: AnalysisContext) -> RcaReport:
    data = _extract_json_object(reply)

    report = report_from_dict(data)
    # The report always refers to the sha that was analysed, never to a branch.
    report.repo = ctx.repo_name
    report.sha = ctx.sha
    report.sha_short = ctx.sha[:9]
    report.role = "root-cause-analyzer"
    report.commit = ctx.commit
    report.repomap = ctx.repomap.as_dict()
    report.training = ctx.training.as_dict()
    if not report.symptom:
        report.symptom = ctx.evidence.exception or "failure reported in the supplied evidence"
    return report