"""The acceptance scenarios, as data.

Both the acceptance suite and the dashboard read this table, so the scenarios a
test proves are exactly the scenarios the UI shows.  Keeping one list is the
point: a dashboard that silently drifts from the suite would report coverage
that does not exist.

Each scenario pairs a repository, an *exact* sha, and a failure evidence file.
Three of them are deliberately awkward, because saying "I don't know" is the
behaviour that matters most for a root cause tool:

* ``stale``   - evidence from a different sha, which cannot occur here;
* ``uncertain`` - evidence that names no code site at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SAMPLE_REPO = PROJECT_ROOT / "fixtures" / "sample-repo"
ALT_REPO = PROJECT_ROOT / "fixtures" / "alt-repo"
EVIDENCE_DIR = PROJECT_ROOT / "evidence"

SAMPLE_BROKEN_SHA = "467b3d80a3a2c7bd0cecd81e44838bb3ff5fdc0c"
SAMPLE_FIXED_SHA = "ad23862538978a8f1e39a251c6882e9b1d29839a"
ALT_REPO_SHA = "f3ab813dcd44e258451682b0da624b4840d56092"


@dataclass(frozen=True)
class Scenario:
    """One diagnosis task against one exact commit."""

    key: str
    title: str
    repo: Path
    sha: str
    evidence: Path
    expect_detector: str | None
    expect_confidence: str | None
    note: str

    @property
    def repo_name(self) -> str:
        return self.repo.name

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "repo": str(self.repo),
            "repo_name": self.repo_name,
            "sha": self.sha,
            "sha_short": self.sha[:9],
            "evidence": str(self.evidence),
            "evidence_name": self.evidence.name,
            "expect_detector": self.expect_detector,
            "expect_confidence": self.expect_confidence,
            "note": self.note,
        }


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="broken",
        title="Optional dict lookup with no default",
        repo=SAMPLE_REPO,
        sha=SAMPLE_BROKEN_SHA,
        evidence=EVIDENCE_DIR / "pytest_failure.txt",
        expect_detector="none-dereference-on-optional-lookup",
        expect_confidence="High",
        note="A traced TypeError. The defect is a missing fallback, not the error itself.",
    ),
    Scenario(
        key="precision",
        title="Decimal scale is never normalised",
        repo=SAMPLE_REPO,
        sha=SAMPLE_FIXED_SHA,
        evidence=EVIDENCE_DIR / "pytest_failure_sha_b.txt",
        expect_detector="unnormalised-numeric-precision",
        expect_confidence="Medium",
        note="The fallback exists here, so the cause is elsewhere: decimal arithmetic "
        "widens the scale and the rendered amount disagrees.",
    ),
    Scenario(
        key="stale",
        title="Evidence that cannot occur at this sha",
        repo=SAMPLE_REPO,
        sha=SAMPLE_FIXED_SHA,
        evidence=EVIDENCE_DIR / "pytest_failure.txt",
        expect_detector="evidence-not-reproducible-at-sha",
        expect_confidence="Low",
        note="The traceback belongs to an earlier commit. The correct answer is to say so, "
        "not to invent a defect.",
    ),
    Scenario(
        key="alt",
        title="Second repository: wrap-around drain order",
        repo=ALT_REPO,
        sha=ALT_REPO_SHA,
        evidence=EVIDENCE_DIR / "alt_repo_failure.txt",
        expect_detector="unclamped-counter-index",
        expect_confidence="Medium",
        note="A different repository and a different failure class.",
    ),
    Scenario(
        key="uncertain",
        title="Evidence that names no code site",
        repo=SAMPLE_REPO,
        sha=SAMPLE_FIXED_SHA,
        evidence=EVIDENCE_DIR / "uncertain_failure.txt",
        expect_detector="insufficient-evidence",
        expect_confidence="Low",
        note="Nothing to trace. The correct answer is insufficient evidence.",
    ),
)

BY_KEY = {s.key: s for s in SCENARIOS}

#: Every sha the fixtures are pinned to.  Asserted at session start so a
#: rewritten fixture history fails loudly instead of silently changing meaning.
PINNED_SHAS: tuple[tuple[Path, str], ...] = (
    (SAMPLE_REPO, SAMPLE_BROKEN_SHA),
    (SAMPLE_REPO, SAMPLE_FIXED_SHA),
    (ALT_REPO, ALT_REPO_SHA),
)