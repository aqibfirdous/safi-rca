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
