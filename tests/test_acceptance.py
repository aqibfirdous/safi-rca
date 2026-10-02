"""Acceptance suite for Product 010.

Ten tests, one per acceptance criterion, in the order the specification lists
them.  Each test is written so that it can only pass if the analyser genuinely
read the repository at the requested sha, cited real source, and left the
repository untouched.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import (
    ALT_REPO,
    ALT_REPO_SHA,
    EVIDENCE_DIR,
    PROJECT_ROOT,
    ROLE_TRAINING,
    SAMPLE_BROKEN_SHA,
    SAMPLE_FIXED_SHA,
    SAMPLE_REPO,
)
from safi_rca import gitutil
from safi_rca.api import temp_root
from safi_rca.runtime.openhands_runtime import LLM_ENV_KEYS

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# 1. Exact sha, not a branch, not HEAD
def test_01_analysis_targets_the_requested_sha(broken_report):
    assert broken_report.sha == SAMPLE_BROKEN_SHA
    assert broken_report.sha != gitutil.head_sha(SAMPLE_REPO)
    assert broken_report.commit["sha"] == SAMPLE_BROKEN_SHA
    assert broken_report.sha_protection["analysed_sha"] == SAMPLE_BROKEN_SHA
    assert "only" in broken_report.sha_protection["scope"]


# 2. Aider RepoMap really produced the map
def test_02_repo_map_is_produced_by_aider(broken_report):
    repomap = broken_report.repomap
    assert repomap["non_empty"] is True
    assert repomap["chars"] > 0
    assert repomap["files_mapped"] > 0
    assert repomap["error"] is None
    assert "aider" in repomap["producer"].lower()


# 3. Repository training markers are used
def test_03_repository_training_is_consulted(broken_report, alt_report):
    for report, marker in ((broken_report, "PLAN-RESOLUTION-1847"), (alt_report, "BATCH-DRAIN-77")):
        text = json.dumps(report.to_dict())
        assert marker in text, f"{marker} missing from the {report.repo} report"
        sources = {fact["path"] for fact in report.training["repo_training"]}
        assert any(name.endswith("AGENTS.md") for name in sources)
        quoted = [item for item in report.evidence if item.kind == "training"]
        assert quoted, "no training fact was quoted as evidence"
        assert any(marker in (item.statement + item.quote) for item in quoted)


# 4. RCA role training is loaded and applied
def test_04_role_training_is_applied(broken_report):
    assert broken_report.role == "root-cause-analyzer"
    origin = broken_report.training["role_training"]["origin"]
    assert ROLE_TRAINING.name in origin, origin
    assert Path(str(origin).rsplit("(", 1)[-1].rstrip(")")).is_file(), origin
    sections = " ".join(fact["section"] for fact in broken_report.training["role_training"]["sections"])
    assert "symptom" in sections.lower()
    for field in ("symptom", "root_cause", "affected_component", "reasoning_summary", "uncertainty", "recommended_next_action"):
        assert getattr(broken_report, field).strip(), f"{field} is empty"
    # The role's own rules are visible in the output: the action is scoped to the
    # analysed commit, and safi-rca states that it changed nothing.
    assert broken_report.sha_short in broken_report.recommended_next_action
    assert "made no change" in broken_report.recommended_next_action
    assert broken_report.read_only == {} or broken_report.read_only.get("clean") in (True, None)


# 5. Symptom and root cause are distinct, and the cause is not the failing line
def test_05_symptom_differs_from_root_cause(broken_report):
    assert broken_report.symptom != broken_report.root_cause
    assert "test_gold_customer_charge_succeeds" in broken_report.symptom
    assert "TypeError" in broken_report.symptom
    # The symptom is the crash; the cause is the missing default upstream of it.
    assert "TypeError" not in broken_report.root_cause
    assert broken_report.root_cause.startswith("PaymentValidator._lookup_plan")
    # the blamed component is the *producing* function, not the line that crashed
    assert "_lookup_plan" in broken_report.affected_component
    assert broken_report.affected_component.split("::")[0].strip().endswith("validator.py")
    assert broken_report.affected_component.split("::")[1].strip().startswith("PaymentValidator._lookup_plan")
    assert broken_report.symptom != broken_report.reasoning_summary


# 6. The known root cause is recovered
def test_06_known_root_cause_is_identified(broken_report):
    assert broken_report.detectors_fired == ["none-dereference-on-optional-lookup"]
    assert broken_report.confidence == "High"
    assert "PaymentValidator._lookup_plan" in broken_report.affected_component
    assert "DEFAULT_PLAN" in broken_report.root_cause
    citations = {(item.file, item.line) for item in broken_report.evidence if item.kind == "source"}
    assert ("src/payment/validator.py", 47) in citations  # the unguarded lookup
    assert ("src/payment/validator.py", 32) in citations  # the unguarded dereference


# 7. Every claim carries concrete evidence
def test_07_every_claim_is_backed_by_evidence(broken_report):
    assert len(broken_report.evidence) >= 5
    kinds = {item.kind for item in broken_report.evidence}
    assert {"source", "training"} <= kinds
    for item in broken_report.evidence:
        assert item.statement.strip(), "evidence item without a statement"
        if item.kind in {"source", "test"}:
            assert item.file and item.line, f"{item.kind} evidence must cite file:line"
    # every source citation must exist at the analysed sha, and quote what it claims
    for item in broken_report.evidence:
        if item.kind == "source":
            exported = _source_at_sha(item.file, SAMPLE_REPO, SAMPLE_BROKEN_SHA)
            assert exported is not None, f"{item.file} is not a file of {SAMPLE_BROKEN_SHA[:9]}"
            text = exported.read_text(encoding="utf-8").splitlines()
            assert 1 <= item.line <= len(text), f"{item.file}:{item.line} is out of range"
            if item.quote.strip():
                # quotes come from ast.unparse, so compare quote-normalised text
                assert _same_source(item.quote, text[item.line - 1]), (
                    f"{item.file}:{item.line} does not match the quote {item.quote!r}"
                )


# 8. Read-only, HEAD unmoved
def test_08_repository_is_never_modified(broken_report, precision_report, alt_report):
    for report in (broken_report, precision_report, alt_report):
        verdict = report.read_only
        assert verdict["clean"] is True
        assert verdict["head_moved_during_analysis"] is False
        assert verdict["head_before"] == verdict["head_after"]
        assert verdict["files_changed"] == []
        assert verdict["head_before"] == gitutil.head_sha(_repo_of(report))
    assert gitutil.is_clean(SAMPLE_REPO)
    assert gitutil.is_clean(ALT_REPO)
    assert "git archive" in broken_report.sha_protection["analysed_from_export_of"]


# 9. A different sha yields a different answer; stale evidence is refused
def test_09_same_repo_different_sha_different_root_cause(precision_report, broken_report, stale_report):
    assert precision_report.sha == SAMPLE_FIXED_SHA
    assert broken_report.sha == SAMPLE_BROKEN_SHA
    assert precision_report.affected_component != broken_report.affected_component
    assert precision_report.detectors_fired == ["unnormalised-numeric-precision"]
    assert "validate" in precision_report.affected_component
    assert "100.0000" in precision_report.root_cause

    # The sha-A traceback cannot occur at sha-B: the report must say so instead
    # of inventing a cause for the current code.
    assert stale_report.detectors_fired == ["evidence-not-reproducible-at-sha"]
    assert stale_report.confidence == "Low"
    assert "_lookup_plan" in stale_report.root_cause
    assert "does not describe the analysed commit" in stale_report.root_cause


# 10. A second repository, and honest uncertainty
def test_10_second_repository_and_uncertainty(alt_report, uncertain_report):
    assert alt_report.sha == ALT_REPO_SHA
    assert alt_report.detectors_fired == ["unclamped-counter-index"]
    assert "RingBuffer.drain" in alt_report.affected_component
    assert "src/ingest/buffer.py" in json.dumps(alt_report.to_dict())
    # The training that drove the answer is that repository's own, not sample-repo's.
    sources = {fact["path"] for fact in alt_report.training["repo_training"]}
    assert all("alt-repo" in name or name.endswith("AGENTS.md") for name in sources)

    assert uncertain_report.detectors_fired == ["insufficient-evidence"]
    assert uncertain_report.confidence == "Low"
    assert uncertain_report.root_cause.startswith("Not determinable")
    assert "no code site" in uncertain_report.affected_component
    assert uncertain_report.uncertainty.strip()
    assert uncertain_report.recommended_next_action.strip()
    assert not [item for item in uncertain_report.evidence if item.kind == "source"], (
        "an uncertain report must not cite source as if it were evidence"
    )


# --- extra guarantees beyond the ten criteria -------------------------------

def test_cli_emits_machine_readable_json(tmp_path):
    out = tmp_path / "report.json"
    result = subprocess.run(
        [
            sys.executable, "-m", "safi_rca", "analyze",
            "--repo", str(SAMPLE_REPO),
            "--sha", SAMPLE_FIXED_SHA,
            "--evidence", str(EVIDENCE_DIR / "pytest_failure_sha_b.txt"),
            "--json-out", str(out),
        ],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["sha"] == SAMPLE_FIXED_SHA
    assert payload["read_only"]["clean"] is True
    assert payload["repomap"]["non_empty"] is True
    assert "ROOT CAUSE" in result.stdout


def test_cli_rejects_unknown_runtime_and_bad_sha():
    bad_sha = subprocess.run(
        [
            sys.executable, "-m", "safi_rca", "analyze",
            "--repo", str(SAMPLE_REPO), "--sha", "deadbeef",
            "--evidence", str(EVIDENCE_DIR / "pytest_failure.txt"),
        ],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert bad_sha.returncode == 2
    assert "safi-rca:" in bad_sha.stderr


def test_openhands_runtime_is_declared_even_when_unavailable():
    from safi_rca.api import describe_runtimes

    runtimes = describe_runtimes()
    assert "openhands" in runtimes
    assert "local-deterministic" in runtimes
    assert runtimes["local-deterministic"]["available"] is True
    assert isinstance(runtimes["openhands"]["available"], bool)
    if not runtimes["openhands"]["available"]:
        assert runtimes["openhands"]["reason"], "an unavailable runtime must explain itself"


def test_openai_shim_keeps_the_real_aider_repomap_working():
    """openhands-sdk requires openai>=2.20; aider 0.16 reads openai.api_base.

    The two cannot be resolved into one dependency set, so the shim is what lets
    the Aider RepoMap (acceptance test 2) keep working next to the agent runtime.
    """
    from safi_rca import openai_compat

    assert openai_compat.install() is True
    import openai

    assert isinstance(openai.api_base, str), "aider dereferences openai.api_base at import time"
    assert "openrouter.ai" not in openai.api_base, "must not masquerade as an openrouter endpoint"

    # And the import aider actually performs at module load must still work.
    from safi_rca.repomap import build_with_aider

    import tempfile

    from safi_rca.api import temp_root

    out = Path(tempfile.mkdtemp(prefix="aider-shim-", dir=temp_root(SAMPLE_REPO)))
    gitutil.export_commit(SAMPLE_REPO, SAMPLE_BROKEN_SHA, out)
    result = build_with_aider(out, gitutil.list_files(SAMPLE_REPO, SAMPLE_BROKEN_SHA), 4096)
    assert result.error is None
    assert result.non_empty
    assert "aider" in result.source.lower()


def test_openhands_agent_is_granted_no_mutating_tools():
    """The RCA agent must be structurally unable to write to the analysed tree."""
    from safi_rca.runtime.openhands_runtime import (
        READ_ONLY_TOOLS,
        _resolve_backend,
        build_read_only_agent,
        granted_tool_names,
    )
    from safi_rca.errors import RuntimeUnavailable

    try:
        backend = _resolve_backend()
    except RuntimeUnavailable as exc:
        pytest.skip(f"openhands not installed: {exc}")

    agent = build_read_only_agent(backend, "anthropic/claude-sonnet-4-5", "sk-not-a-real-key")

    # `tools=[]` plus the SDK defaults is what keeps each tool exactly once; the
    # effective set, not `agent.tools`, is what the model may actually call.
    granted = granted_tool_names(agent)
    assert granted == set(READ_ONLY_TOOLS)
    assert [getattr(tool, "name", str(tool)) for tool in (agent.tools or [])] == []

    mutating = ("bash", "exec", "write", "edit", "terminal", "str_replace", "patch", "file")
    for name in granted:
        lowered = name.lower()
        assert not any(bad in lowered for bad in mutating), f"mutating tool granted: {name}"


def test_openhands_reply_is_read_from_either_answer_shape():
    """The agent's answer arrives via `finish` *or* as plain assistant text.

    Which one a model emits is its own choice -- observed both ways against a
    live model -- so the extractor has to handle each.  Missing either shape
    turns a successful, correctly-reasoned run into a spurious
    ``RuntimeUnavailable``.
    """
    from openhands.sdk.event import ActionEvent, MessageEvent
    from openhands.sdk.llm import Message, MessageToolCall, TextContent
    from openhands.sdk.tool.builtins.finish import FinishAction

    from safi_rca.errors import RuntimeUnavailable
    from safi_rca.runtime.openhands_runtime import _final_agent_text

    answer = '{"root_cause": "fallback missing"}'

    class FakeState:
        def __init__(self, events):
            self.events = events

    class FakeConversation:
        def __init__(self, events):
            self.state = FakeState(events)

    # Shape 1: a plain agent message, with no `finish` call at all.
    as_message = MessageEvent(
        source="agent",
        llm_message=Message(role="assistant", content=[TextContent(text=answer)]),
    )
    assert _final_agent_text(FakeConversation([as_message])) == answer

    # Shape 2: the `finish` tool call, whose action carries the message and
    # deliberately renders an empty observation.
    finish_action = ActionEvent(
        source="agent",
        tool_name="FinishTool",
        action=FinishAction(message=answer),
        thought=[TextContent(text="the analysis is complete")],
        tool_call_id="call-1",
        tool_call=MessageToolCall(
            id="call-1",
            name="finish",
            arguments=json.dumps({"message": answer}),
            origin="completion",
        ),
        llm_response_id="resp-1",
    )
    assert _final_agent_text(FakeConversation([finish_action])) == answer

    # A user message is never mistaken for the answer, and an agent that said
    # nothing is reported rather than silently yielding an empty report.
    user_event = MessageEvent(
        source="user",
        llm_message=Message(role="user", content=[TextContent(text="do the RCA")]),
    )
    with pytest.raises(RuntimeUnavailable):
        _final_agent_text(FakeConversation([user_event]))


def test_parsed_report_carries_real_provenance(broken_report):
    """A parsed model reply must keep the repo-map and training provenance.

    ``RcaReport`` defaults these to ``{}``, so a mistyped field name on the
    report we build from the model's JSON silently drops the evidence that a
    RepoMap was used at all -- the run still looks successful.  Assert the
    provenance is carried, not just that the call returns something.
    """
    from safi_rca.context import build_context
    from safi_rca.runtime.openhands_runtime import _parse_report

    with _exported_tree(SAMPLE_REPO, SAMPLE_BROKEN_SHA) as export:
        ctx = build_context(SAMPLE_REPO, SAMPLE_BROKEN_SHA, EVIDENCE_DIR / "pytest_failure.txt", export)

    reply = json.dumps(
        {
            "symptom": "TypeError",
            "root_cause": "missing DEFAULT_PLAN fallback in _lookup_plan",
            "affected_component": "src/payment/validator.py",
            "confidence": "High",
            "uncertainty": "None",
        }
    )
    report = _parse_report(reply, ctx)

    assert report.repomap, "repo-map provenance was dropped"
    assert report.repomap["producer"], "repo-map must name the producer that ran"
    assert report.repomap["files_mapped"] > 0, "repo-map must actually map files"
    assert report.repomap["non_empty"] is True
    assert report.training, "training provenance was dropped"
    assert report.sha == SAMPLE_BROKEN_SHA
    assert report.repo == "sample-repo"


@contextmanager
def _exported_tree(repo: Path, sha: str):
    """Export `sha` to a scratch dir, with no checkout of the user's repo."""

    with tempfile.TemporaryDirectory(dir=temp_root(repo)) as tmp:
        export = Path(tmp) / "export"
        gitutil.export_commit(repo, sha, export)
        yield export


def test_gateway_credentials_route_to_the_right_provider(monkeypatch):
    """OpenRouter and friends: the key and the model prefix must agree.

    Sending an OpenRouter key to a model named ``anthropic/...`` fails deep
    inside litellm with an opaque auth error, so the mismatch is caught before
    the run and reported by ``runtimes``.
    """
    from safi_rca.runtime.openhands_runtime import credential_mismatch

    for key in LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert "OPENROUTER_API_KEY" in LLM_ENV_KEYS, "OpenRouter is a supported credential"

    assert credential_mismatch("OPENROUTER_API_KEY", "openrouter/anthropic/claude-sonnet-4.5") is None
    caught = credential_mismatch("OPENROUTER_API_KEY", "anthropic/claude-sonnet-4-5")
    assert caught and "openrouter/" in caught, "an OpenRouter key with a non-OpenRouter model must be flagged"

    # The same applies to every provider, and the message must name the provider
    # actually expected -- not just say "OpenRouter" for any key.
    gemini = credential_mismatch("GEMINI_API_KEY", "anthropic/claude-sonnet-4-5")
    assert gemini and "gemini/" in gemini and "openrouter" not in gemini.lower()
    assert credential_mismatch("GEMINI_API_KEY", "gemini/gemini-3-flash-preview") is None

    # A model that names no provider at all is the default and is not flagged,
    # because the operator may be pointing at a proxy.
    assert credential_mismatch("ANTHROPIC_API_KEY", "anthropic/claude-sonnet-4-5") is None
    assert credential_mismatch("LLM_API_KEY", "anything/at-all") is None


# --- helpers -----------------------------------------------------------------

_SOURCE_CACHE: dict[tuple[str, str], Path | None] = {}


def _source_at_sha(rel_path: str, repo: Path, sha: str) -> Path | None:
    key = (str(repo), sha)
    if key not in _SOURCE_CACHE:
        import tempfile

        from safi_rca.api import temp_root

        out = Path(tempfile.mkdtemp(prefix="acceptance-", dir=temp_root(repo)))
        gitutil.export_commit(repo, sha, out)
        _SOURCE_CACHE[key] = out
    root = _SOURCE_CACHE[key]
    candidate = root / rel_path
    return candidate if candidate.is_file() else None


def _repo_of(report) -> Path:
    return ALT_REPO if "alt-repo" in str(report.repo) else SAMPLE_REPO


def _same_source(left: str, right: str) -> bool:
    """Compare two renderings of one source line, ignoring quote style/whitespace.

    Citations are rendered with ``ast.unparse`` (so ``customer["tier"]`` becomes
    ``customer['tier']``), the file on disk uses the author's own quoting.
    """

    def norm(text: str) -> str:
        text = re.sub(r"[\"']", "", text)
        return re.sub(r"\s+", " ", text).strip()

    return norm(left) in norm(right)
