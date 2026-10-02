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

# Never read a developer's real credential during a test run: a live model call
# from the suite would be slow, billable, and non-deterministic.
os.environ["SAFI_RCA_NO_DOTENV"] = "1"

# The scenarios are one shared definition, read by this suite and by the
# dashboard.  A UI that drifted from the suite would claim coverage the suite
# does not provide, so there is a single list.
from safi_rca.scenarios import (  # noqa: E402
    ALT_REPO,
    ALT_REPO_SHA,
    BY_KEY,
    EVIDENCE_DIR,
    PINNED_SHAS,
    SAMPLE_BROKEN_SHA,
    SAMPLE_FIXED_SHA,
    SAMPLE_REPO,
)

ROLE_TRAINING = PROJECT_ROOT / ".agents" / "skills" / "root-cause-analyzer.md"


@pytest.fixture(scope="session", autouse=True)
def fixtures_are_pinned() -> None:
    """Fail fast if a fixture repository no longer matches the recorded shas."""
    from safi_rca import gitutil

    for repo, sha in PINNED_SHAS:
        assert gitutil.resolve_sha(repo, sha) == sha, f"{repo} no longer contains {sha}"


@pytest.fixture(scope="session")
def broken_report():
    """Known root cause: missing default in an optional dict lookup."""
    from safi_rca.api import analyze

    s = BY_KEY["broken"]
    return analyze(repo=s.repo, sha=s.sha, evidence=s.evidence, runtime="local")


@pytest.fixture(scope="session")
def precision_report():
    """Same repository, later sha: the fallback is in place, a precision defect is not."""
    from safi_rca.api import analyze

    s = BY_KEY["precision"]
    return analyze(repo=s.repo, sha=s.sha, evidence=s.evidence, runtime="local")


@pytest.fixture(scope="session")
def alt_report():
    """Second repository: a completely different failure class."""
    from safi_rca.api import analyze

    s = BY_KEY["alt"]
    return analyze(repo=s.repo, sha=s.sha, evidence=s.evidence, runtime="local")


@pytest.fixture(scope="session")
def uncertain_report():
    """Evidence that names no code site at all."""
    from safi_rca.api import analyze

    s = BY_KEY["uncertain"]
    return analyze(repo=s.repo, sha=s.sha, evidence=s.evidence, runtime="local")


@pytest.fixture(scope="session")
def stale_report():
    """SHA-B analysed against SHA-A evidence: the traceback cannot occur there."""
    from safi_rca.api import analyze

    s = BY_KEY["stale"]
    return analyze(repo=s.repo, sha=s.sha, evidence=s.evidence, runtime="local")
