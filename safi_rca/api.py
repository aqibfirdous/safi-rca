"""Programmatic entry point: :func:`analyze`."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from . import gitutil
from .context import build_context
from .readonly import ReadOnlyGuard
from .report import RcaReport
from .runtime import get_runtime

DEFAULT_MAP_TOKENS = 4096


def temp_root(repo_path: Path) -> Path:
    """Directory that holds the read-only ``git archive`` exports.

    Defaults to a scratch folder on the *same drive as the repository* so that
    analysis never spills onto another volume.  ``SAFI_RCA_TMP_DIR`` overrides
    the location entirely.
    """
    override = os.environ.get("SAFI_RCA_TMP_DIR")
    if override:
        root = Path(override).expanduser()
    else:
        root = gitutil.scratch_root(repo_path)
    root.mkdir(parents=True, exist_ok=True)
    return root


def analyze(
    repo: str | Path,
    sha: str,
    evidence: str | Path,
    *,
    runtime: str = "auto",
    map_tokens: int = DEFAULT_MAP_TOKENS,
    keep_export: bool = False,
) -> RcaReport:
    """Diagnose ``evidence`` against ``sha`` of ``repo``.  Read-only."""
    repo_path = gitutil.verify_repository(Path(repo))
    resolved_sha = gitutil.resolve_sha(repo_path, sha)

    guard = ReadOnlyGuard(repo_path)
    export_root = Path(tempfile.mkdtemp(prefix="safi-rca-", dir=temp_root(repo_path)))
    export_dir = export_root / resolved_sha[:9]
    try:
        gitutil.export_commit(repo_path, resolved_sha, export_dir)
        ctx = build_context(repo_path, resolved_sha, evidence, export_dir, map_tokens=map_tokens)

        agent = get_runtime(runtime)
        usable, reason = agent.availability()
        result = agent.run(ctx)
        report = result.report

        verdict = guard.verify()
        report.read_only = verdict.as_dict()
        report.runtime = {
            "selected": result.runtime,
            "requested": runtime,
            "available": usable,
            "availability": reason,
            "notes": result.notes,
            **result.details,
        }
        report.sha_protection = {
            "analysed_sha": resolved_sha,
            "scope": f"This diagnosis applies to {resolved_sha} only. It is not valid for any other commit.",
            "head_at_analysis_start": verdict.head_before,
            "head_after_analysis": verdict.head_after,
            "head_moved_during_analysis": verdict.head_moved,
            "worktree_dirty_at_start": ctx.dirty_at_start,
            "analysed_from_export_of": "git archive (read-only, no checkout)",
        }
        if not verdict.clean:
            report.limitations.append(
                f"repository HEAD or working tree changed during analysis; this report still describes {resolved_sha[:9]} only"
            )
        return report
    finally:
        if keep_export:
            print(f"[safi-rca] export kept at {export_dir}")
        else:
            shutil.rmtree(export_root, ignore_errors=True)


def describe_runtimes() -> dict:
    from .runtime.local_runtime import LocalDeterministicRuntime
    from .runtime.openhands_runtime import OpenHandsRuntime

    out = {}
    for runtime in (OpenHandsRuntime(), LocalDeterministicRuntime()):
        usable, reason = runtime.availability()
        out[runtime.name] = {"available": usable, "reason": reason}
    return out
