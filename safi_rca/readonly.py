"""The read-only invariant, enforced rather than promised.

``safi-rca`` takes a fingerprint of the repository before analysis and takes it
again afterwards.  If HEAD moved, or if any tracked/untracked byte changed, the
report is returned *and* flagged as violated so that the caller can see it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from . import gitutil


def _fingerprint(repo: Path) -> dict:
    entries = {}
    status = gitutil.status_porcelain(repo)
    for line in status:
        path = gitutil.normalise(line[3:].split(" -> ")[-1])
        full = repo / path
        if full.is_file():
            entries[path] = hashlib.sha256(full.read_bytes()).hexdigest()[:16]
        else:
            entries[path] = "<dir-or-deleted>"
    head = gitutil.head_sha(repo)
    return {"head": head, "status": sorted(status), "blobs": entries}


@dataclass
class ReadOnlyVerdict:
    head_before: str | None
    head_after: str | None
    status_before: list[str] = field(default_factory=list)
    status_after: list[str] = field(default_factory=list)
    blobs_before: dict = field(default_factory=dict)
    blobs_after: dict = field(default_factory=dict)

    @property
    def head_moved(self) -> bool:
        return self.head_before != self.head_after

    @property
    def files_changed(self) -> list[str]:
        changed = set(self.status_before) ^ set(self.status_after)
        changed |= {p for p in self.blobs_before if self.blobs_before.get(p) != self.blobs_after.get(p)}
        return sorted(changed)

    @property
    def clean(self) -> bool:
        return not self.head_moved and not self.files_changed

    def as_dict(self) -> dict:
        return {
            "verified": True,
            "clean": self.clean,
            "head_before": self.head_before,
            "head_after": self.head_after,
            "head_moved_during_analysis": self.head_moved,
            "git_status_before": self.status_before,
            "git_status_after": self.status_after,
            "files_changed": self.files_changed,
        }


class ReadOnlyGuard:
    """Context manager guarding one analysis run."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.before = _fingerprint(repo)

    def verify(self) -> ReadOnlyVerdict:
        after = _fingerprint(self.repo)
        return ReadOnlyVerdict(
            head_before=self.before["head"],
            head_after=after["head"],
            status_before=self.before["status"],
            status_after=after["status"],
            blobs_before=self.before["blobs"],
            blobs_after=after["blobs"],
        )

    def __enter__(self) -> "ReadOnlyGuard":
        return self

    def __exit__(self, *_exc) -> bool:
        return False
