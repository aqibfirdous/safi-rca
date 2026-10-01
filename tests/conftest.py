"""Shared fixtures: every acceptance scenario is analysed against a *pinned* sha.

The shas below are asserted against the fixture repositories at session start,
so a rewritten fixture history fails loudly instead of silently changing what
the suite proves.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Keep every temporary artefact (git archive exports) on the project drive.
os.environ.setdefault("SAFI_RCA_TMP_DIR", str(PROJECT_ROOT / ".tmp"))

SAMPLE_REPO = PROJECT_ROOT / "fixtures" / "sample-repo"
ALT_REPO = PROJECT_ROOT / "fixtures" / "alt-repo"
EVIDENCE_DIR = PROJECT_ROOT / "evidence"

SAMPLE_BROKEN_SHA = "467b3d80a3a2c7bd0cecd81e44838bb3ff5fdc0c"
SAMPLE_FIXED_SHA = "ad23862538978a8f1e39a251c6882e9b1d29839a"
ALT_REPO_SHA = "f3ab813dcd44e258451682b0da624b4840d56092"

ROLE_TRAINING = PROJECT_ROOT / ".agents" / "skills" / "root-cause-analyzer.md"


@pytest.fixture(scope="session", autouse=True)
def fixtures_are_pinned() -> None:
    """Fail fast if a fixture repository no longer matches the recorded shas."""
    from safi_rca import gitutil

    for repo, sha in (
        (SAMPLE_REPO, SAMPLE_BROKEN_SHA),
        (SAMPLE_REPO, SAMPLE_FIXED_SHA),
        (ALT_REPO, ALT_REPO_SHA),
    ):
        assert gitutil.resolve_sha(repo, sha) == sha, f"{repo} no longer contains {sha}"


@pytest.fixture(scope="session")
def broken_report():
    """Known root cause: missing default in an optional dict lookup."""
    from safi_rca.api import analyze

    return analyze(
        repo=SAMPLE_REPO,
        sha=SAMPLE_BROKEN_SHA,
        evidence=EVIDENCE_DIR / "pytest_failure.txt",
        runtime="local",
    )


@pytest.fixture(scope="session")
def precision_report():
    """Same repository, later sha: the fallback is in place, a precision defect is not."""
    from safi_rca.api import analyze

    return analyze(
        repo=SAMPLE_REPO,
        sha=SAMPLE_FIXED_SHA,
        evidence=EVIDENCE_DIR / "pytest_failure_sha_b.txt",
        runtime="local",
    )


@pytest.fixture(scope="session")
def alt_report():
    """Second repository: a completely different failure class."""
    from safi_rca.api import analyze

    return analyze(repo=ALT_REPO, sha=ALT_REPO_SHA, evidence=EVIDENCE_DIR / "alt_repo_failure.txt", runtime="local")


@pytest.fixture(scope="session")
def uncertain_report():
    """Evidence that names no code site at all."""
    from safi_rca.api import analyze

    return analyze(
        repo=SAMPLE_REPO,
        sha=SAMPLE_FIXED_SHA,
        evidence=EVIDENCE_DIR / "uncertain_failure.txt",
        runtime="local",
    )


@pytest.fixture(scope="session")
def stale_report():
    """SHA-B analysed against SHA-A evidence: the traceback cannot occur there."""
    from safi_rca.api import analyze

    return analyze(
        repo=SAMPLE_REPO,
        sha=SAMPLE_FIXED_SHA,
        evidence=EVIDENCE_DIR / "pytest_failure.txt",
        runtime="local",
    )
