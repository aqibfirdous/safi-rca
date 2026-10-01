"""Read-only git access.

Every function here shells out to ``git`` with plumbing commands only.  Nothing
in this module writes to the repository, its index, its history or its refs.
The only thing safi-rca ever materialises is a throw-away *export* of one exact
commit (see :func:`export_commit`) into a scratch directory outside the repo.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import GitError, InputError

_SHA_RE = re.compile(r"^[0-9a-f]{4,40}$")
_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".ico"}
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
_TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".sql", ".sh"}


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    short_sha: str
    subject: str
    author: str
    authored_at: str

    def as_dict(self) -> dict:
        return {
            "sha": self.sha,
            "short_sha": self.short_sha,
            "subject": self.subject,
            "author": self.author,
            "authored_at": self.authored_at,
        }


def _git(repo: Path, *args: str, binary: bool = False) -> bytes | str:
    cmd = ["git", "-C", str(repo), *args]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    if binary:
        return proc.stdout
    return proc.stdout.decode("utf-8", "replace")


def normalise(path: str | os.PathLike) -> str:
    """Repo-relative posix path, tolerant of Windows separators in evidence."""
    text = str(path).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def verify_repository(repo: Path) -> Path:
    repo = Path(repo).expanduser().resolve()
    if not repo.is_dir():
        raise InputError(f"repo is not a directory: {repo}")
    inside = _git(repo, "rev-parse", "--is-inside-work-tree").strip()
    if inside != "true":
        raise InputError(f"not a git work tree: {repo}")
    return repo


def resolve_sha(repo: Path, sha: str) -> str:
    """Resolve ``sha`` to a full 40 char commit id.  Never guesses a branch."""
    sha = (sha or "").strip()
    if not sha:
        raise InputError("--sha is required: an exact git sha must be given")
    if not _SHA_RE.match(sha.lower()):
        raise InputError(f"--sha must be a hex commit id, got {sha!r}")
    resolved = _git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}").strip()
    if not resolved:
        raise GitError(f"commit not found in {repo}: {sha}")
    return resolved


def commit_info(repo: Path, sha: str) -> CommitInfo:
    fmt = "%H%x1f%h%x1f%s%x1f%an%x1f%aI"
    out = _git(repo, "show", "-s", f"--format={fmt}", sha).strip()
    full, short, subject, author, authored = out.split("\x1f")
    return CommitInfo(full, short, subject, author, authored)


def head_sha(repo: Path) -> str | None:
    try:
        return _git(repo, "rev-parse", "HEAD").strip() or None
    except GitError:
        return None


def status_porcelain(repo: Path) -> list[str]:
    return [line for line in _git(repo, "status", "--porcelain=v1", "--untracked-files=all").splitlines() if line.strip()]


def list_files(repo: Path, sha: str) -> list[str]:
    out = _git(repo, "ls-tree", "-r", "--name-only", sha)
    return [normalise(line) for line in out.splitlines() if line.strip()]


def show_file(repo: Path, sha: str, path: str) -> str | None:
    try:
        return _git(repo, "show", f"{sha}:{normalise(path)}")
    except GitError:
        return None


def show_file_at_line(repo: Path, sha: str, path: str, line: int) -> str | None:
    text = show_file(repo, sha, path)
    if text is None:
        return None
    lines = text.splitlines()
    if 1 <= line <= len(lines):
        return lines[line - 1]
    return None


def blame_line(repo: Path, sha: str, path: str, line: int) -> str | None:
    """``git blame`` porcelain-ish output for a single line (read only)."""
    fmt = "%H%x1f%an%x1f%s"
    try:
        out = _git(repo, "blame", "-L", f"{line},{line}", f"--line-porcelain", sha, "--", normalise(path))
    except GitError:
        return None
    for raw in out.splitlines():
        if raw.startswith(fmt.split("\x1f")[0] + " "):
            parts = raw.split(" ", 1)
            if len(parts) == 2:
                commit, rest = parts[0], parts[1].lstrip("0123456789")
                meta = {}
                for entry in rest.split("\n"):
                    if "\t" in entry:
                        entry = entry.split("\t", 1)[1]
                    if " " in entry:
                        key, _, val = entry.partition(" ")
                        meta[key] = val
                return f"{commit[:9]} {meta.get('author', '?')} {meta.get('summary', '')}".strip()
    return None


def recent_history(repo: Path, sha: str, limit: int = 5) -> list[str]:
    try:
        out = _git(repo, "log", f"-{limit}", "--format=%h %ad %s", "--date=short", sha)
    except GitError:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def changed_files(repo: Path, sha: str, limit: int = 40) -> list[str]:
    """Files touched by the tip commit -- useful when evidence looks stale."""
    try:
        out = _git(repo, "show", "--name-only", "--format=", sha)
    except GitError:
        return []
    return [normalise(line) for line in out.splitlines() if line.strip()][:limit]


def is_text_candidate(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    return suffix in _TEXT_SUFFIXES or suffix not in _BINARY_SUFFIXES


def is_clean(repo: Path) -> bool:
    """True when nothing in the index or the working tree differs from HEAD."""
    return not status_porcelain(repo)


def scratch_root(repo: Path) -> Path:
    """Writable scratch directory on the same drive as ``repo``."""
    root = Path(f"{repo.drive or 'B:'}\\") / "safi-rca-tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def export_commit(repo: Path, sha: str, dest: Path | None = None, scratch: Path | None = None) -> Path:
    """Materialise the tree of ``sha`` into a scratch directory.

    ``git archive`` is used instead of a checkout on purpose: it cannot touch the
    working tree, the index, HEAD or any ref, and it gives the analyzer the
    exact committed bytes even when the work tree is dirty or on another branch.
    """
    if dest is None:
        root = scratch or scratch_root(repo)
        dest = Path(tempfile.mkdtemp(prefix="safi-rca-export-", dir=root))
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    tar_bytes = _git(repo, "archive", "--format=tar", sha, binary=True)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
        for member in tar:
            name = normalise(member.name)
            if not name or name.startswith("/") or ".." in name.split("/"):
                continue
            if any(part in _SKIP_DIRS for part in name.split("/")[:-1]):
                continue
            tar.extract(member, path=dest, filter="data")
    return dest


def git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False
