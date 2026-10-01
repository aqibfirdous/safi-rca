"""Training loaders.

Two independent kinds of training live in git and are loaded separately:

* **repository training** -- architecture, component boundaries, invariants,
  build/test commands, diagnostic conventions, important files.  Conventional
  locations: ``AGENTS.md``, ``CLAUDE.md``, ``.agents/skills/*.md``.
* **role training** -- the RCA role itself, ``root-cause-analyzer.md``.  It is
  editable independently of repository training; see :func:`load_role_training`
  for the resolution order.

Nothing in this module writes to the repository.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_TRAINING_GLOBS = (
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".agents/skills/*.md",
    "docs/agents/*.md",
    ".github/copilot-instructions.md",
)

ROLE_SKILL_NAME = "root-cause-analyzer"
ROLE_SKILL_REPO_PATHS = (
    f".agents/skills/{ROLE_SKILL_NAME}.md",
    f".agents/skills/{ROLE_SKILL_NAME}/SKILL.md",
    f".agents/skills/{ROLE_SKILL_NAME}/role.md",
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BACKTICK_RE = re.compile(r"`([^`]{1,80})`")
_MARKER_RE = re.compile(r"\b([A-Z][A-Z0-9]*-[A-Z0-9-]{2,})\b")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")

BUILTIN_ROLE_SKILL = """# root-cause-analyzer (built-in fallback)

Remain read-only. Distinguish symptom from root cause. Inspect the evidence
before concluding. Cite files, functions and tests. State uncertainty. Never
invent an unsupported cause. Recommend the next action without editing code.
"""


@dataclass
class TrainingDoc:
    kind: str            # "repo" | "role"
    path: str            # repo-relative path
    origin: str          # where it was loaded from
    text: str
    sha: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "path": self.path, "origin": self.origin, "sha": self.sha}


@dataclass
class TrainingFact:
    text: str
    source: str
    line: int
    section: str = ""
    identifiers: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "fact": self.text,
            "source": self.source,
            "line": self.line,
            "section": self.section,
            "identifiers": self.identifiers,
            "markers": self.markers,
        }

    def cite(self) -> str:
        return f"{self.source}:{self.line}"


def _parse_facts(doc: TrainingDoc) -> list[TrainingFact]:
    facts: list[TrainingFact] = []
    section = ""
    buffer: list[str] = []
    start_line = 1
    seen_text: set[int] = set()

    def flush(line_no: int) -> None:
        nonlocal buffer
        text = " ".join(part.strip() for part in buffer if part.strip())
        buffer = []
        if len(text) < 4:
            return
        digest = hash(text)
        if digest in seen_text:
            return
        seen_text.add(digest)
        identifiers = sorted({m.strip() for m in _BACKTICK_RE.findall(text)})
        markers = sorted(set(_MARKER_RE.findall(text)))
        facts.append(
            TrainingFact(
                text=text,
                source=doc.path,
                line=line_no,
                section=section,
                identifiers=identifiers,
                markers=markers,
            )
        )

    in_code = False
    seen_paths: set[str] = set()
    seen_text: set[int] = set()
    for number, raw in enumerate(doc.text.splitlines(), start=1):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if line.lstrip().startswith("|") or set(line.strip()) <= {"-", "|", ":", " "}:
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush(start_line)
            section = heading.group(2).strip()
            start_line = number
            continue
        if not line.strip():
            flush(start_line)
            continue
        if _BULLET_RE.match(line):
            flush(start_line)
            buffer = [line]
            start_line = number
            continue
        if buffer:
            buffer.append(line)
        else:
            buffer = [line]
            start_line = number
    flush(start_line)
    return [f for f in facts if len(f.text) > 3]


@dataclass
class TrainingSet:
    repo_docs: list[TrainingDoc] = field(default_factory=list)
    role_doc: TrainingDoc | None = None

    @property
    def repo_facts(self) -> list[TrainingFact]:
        facts: list[TrainingFact] = []
        for doc in self.repo_docs:
            facts.extend(_parse_facts(doc))
        return facts

    @property
    def role_facts(self) -> list[TrainingFact]:
        if self.role_doc is None:
            doc = TrainingDoc(kind="role", path="<built-in>", origin="built-in", text=BUILTIN_ROLE_SKILL, sha="")
            return _parse_facts(doc)
        return _parse_facts(self.role_doc)

    def role_text(self) -> str:
        return self.role_doc.text if self.role_doc else BUILTIN_ROLE_SKILL

    def as_dict(self) -> dict:
        role = self.role_doc.as_dict() if self.role_doc else {"kind": "role", "path": "<built-in>", "origin": "built-in", "sha": ""}
        # the parsed sections prove the role text was actually understood, not
        # merely located
        role["sections"] = [fact.as_dict() for fact in self.role_facts]
        return {
            "repo_training": [d.as_dict() for d in self.repo_docs],
            "role_training": role,
            "repo_fact_count": len(self.repo_facts),
            "role_fact_count": len(self.role_facts),
        }


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def load_repo_training(export_root: Path, repo_path: Path, sha: str) -> list[TrainingDoc]:
    """Load repository training from the exported tree at ``sha``.

    Only files that exist *at the analysed sha* are loaded, so a diagnosis can
    never be justified by training text that does not exist in that version.
    """
    docs: list[TrainingDoc] = []
    seen: set[str] = set()
    seen_digests: set[int] = set()
    for pattern in REPO_TRAINING_GLOBS:
        for candidate in sorted(export_root.glob(pattern)):
            if not candidate.is_file():
                continue
            rel = candidate.relative_to(export_root).as_posix()
            if rel.lower() in seen or rel.endswith(ROLE_SKILL_NAME + ".md"):
                continue
            text = _read(candidate)
            digest = hash(text)
            if digest in seen_digests:
                continue
            seen.add(rel.lower())
            seen_digests.add(digest)
            docs.append(
                TrainingDoc(
                    kind="repo",
                    path=rel,
                    origin=f"repo@{sha[:7]}",
                    text=text,
                    sha=sha,
                )
            )
    # The live work tree is only consulted for training files that the commit
    # itself does not carry, which keeps behaviour sane for un-committed setup.
    if not docs:
        for pattern in REPO_TRAINING_GLOBS:
            for candidate in sorted(repo_path.glob(pattern)):
                if candidate.is_file():
                    rel = candidate.relative_to(repo_path).as_posix()
                    text = _read(candidate)
                    if rel.endswith(ROLE_SKILL_NAME + ".md") or rel.lower() in seen or hash(text) in seen_digests:
                        continue
                    seen.add(rel.lower())
                    seen_digests.add(hash(text))
                    docs.append(
                        TrainingDoc(kind="repo", path=rel, origin="worktree", text=text, sha=sha)
                    )
    return docs


def load_role_training(export_root: Path, repo_path: Path, sha: str) -> TrainingDoc:
    """Resolve the RCA role training.

    Order (first hit wins)::

        1. <repo>/.agents/skills/root-cause-analyzer.md       (repo pinned, wins)
        2. $SAFI_RCA_ROLE_SKILL                                 (operator override)
        3. <safi-rca checkout>/.agents/skills/root-cause-analyzer.md
        4. built-in minimal role
    """
    for rel in ROLE_SKILL_REPO_PATHS:
        for root, origin in ((export_root, f"repo@{sha[:7]}"), (repo_path, "worktree")):
            candidate = root / rel
            if candidate.is_file():
                return TrainingDoc(kind="role", path=rel, origin=origin, text=_read(candidate), sha=sha)

    override = os.environ.get("SAFI_RCA_ROLE_SKILL")
    if override and Path(override).is_file():
        return TrainingDoc(kind="role", path=override, origin="env:SAFI_RCA_ROLE_SKILL", text=_read(Path(override)), sha=sha)

    packaged = Path(__file__).resolve().parents[1] / ".agents" / "skills" / f"{ROLE_SKILL_NAME}.md"
    if packaged.is_file():
        return TrainingDoc(
            kind="role",
            path=".agents/skills/root-cause-analyzer.md",
            origin=f"safi-rca packaged role ({packaged})",
            text=_read(packaged),
            sha=sha,
        )

    return TrainingDoc(kind="role", path="<built-in>", origin="built-in", text=BUILTIN_ROLE_SKILL, sha=sha)


def load_training(export_root: Path, repo_path: Path, sha: str) -> TrainingSet:
    return TrainingSet(
        repo_docs=load_repo_training(export_root, repo_path, sha),
        role_doc=load_role_training(export_root, repo_path, sha),
    )


def identifiers_in(text: str) -> set[str]:
    return set(_WORD_RE.findall(text))


def select_facts(
    facts: list[TrainingFact],
    identifiers: set[str],
    paths: set[str],
    limit: int = 12,
) -> list[TrainingFact]:
    """Facts that are relevant to the failing component.

    A fact is relevant when it mentions an identifier or a repo-relative path
    that belongs to the failing component, or when it carries a marker token and
    also mentions one of those identifiers.  Unrelated prose is dropped so the
    analyzer does not drown in training text.
    """
    identifiers = {i.lower() for i in identifiers}
    scored: list[tuple[int, TrainingFact]] = []
    for fact in facts:
        haystack = fact.text.lower()
        ident_hits = {i for i in identifiers if i in haystack}
        path_hits = {p for p in paths if p.lower() in haystack}
        marker_hit = bool(fact.markers) and (ident_hits or path_hits)
        if not ident_hits and not path_hits and not marker_hit:
            continue
        score = 3 * len(path_hits) + len(ident_hits) + (2 if marker_hit else 0)
        scored.append((score, fact))
    scored.sort(key=lambda item: (-item[0], item[1].source, item[1].line))
    return [fact for _score, fact in scored[:limit]]
