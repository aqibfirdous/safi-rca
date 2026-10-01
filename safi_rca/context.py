"""Context assembly.

Repo training + RCA role training + Aider RepoMap + relevant source at the
exact sha + the failure evidence -> one prompt payload for the agent runtime.

The whole repository is never pushed into the context: the RepoMap carries the
structure and only the files that the evidence and the training actually point
at are expanded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import gitutil, repomap as repomap_mod, training as training_mod
from .evidence import FailureEvidence, load_evidence
from .symbols import SymbolIndex

MAX_SOURCE_FILES = 14
MAX_SOURCE_CHARS = 24000


@dataclass
class SourceSlice:
    path: str
    reason: str
    text: str
    truncated: bool = False

    def as_dict(self) -> dict:
        return {"file": self.path, "reason": self.reason, "chars": len(self.text), "truncated": self.truncated}


@dataclass
class AnalysisContext:
    repo_path: Path
    repo_name: str
    sha: str
    commit: dict
    export_root: Path
    files: list[str]
    evidence: FailureEvidence
    training: training_mod.TrainingSet
    repomap: repomap_mod.RepoMapResult
    index: SymbolIndex
    source_slices: list[SourceSlice] = field(default_factory=list)
    resolved_frames: list[dict] = field(default_factory=list)
    head_at_start: str | None = None
    dirty_at_start: bool = False
    built_at: str = ""

    # ------------------------------------------------------------------ facts
    @property
    def repo_training_text(self) -> str:
        return "\n\n".join(f"### {doc.path} ({doc.origin})\n{doc.text}" for doc in self.training.repo_docs)

    @property
    def role_training_text(self) -> str:
        return self.training.role_text()

    def identifiers_of_interest(self) -> set[str]:
        names: set[str] = set(self.repomap.identifiers)
        for frame in self.resolved_frames:
            if frame.get("symbol"):
                names.add(str(frame["symbol"]).split(".")[-1])
            if frame.get("function"):
                names.add(str(frame["function"]))
        for name in self.index.functions:
            if name in self.evidence.text:
                names.add(name)
        return {n for n in names if len(n) > 2}

    def files_of_interest(self) -> set[str]:
        return {frame["file"] for frame in self.resolved_frames if frame.get("file")}

    # ----------------------------------------------------------------- prompt
    def render_prompt(self) -> str:
        parts = [
            "# Root cause analysis task",
            "",
            f"Repository      : {self.repo_name}",
            f"Exact git sha   : {self.sha}",
            f"Commit subject  : {self.commit.get('subject', '')}",
            f"Authored        : {self.commit.get('authored_at', '')}",
            "",
            "You are diagnosing ONE reported failure against the tree of the exact commit",
            "above. Stay read-only. Do not propose or apply edits as if you were fixing;",
            "only describe the next action a human should take.",
            "",
            "## RCA ROLE TRAINING (mandatory)",
            self.role_training_text,
            "",
            "## REPOSITORY TRAINING (mandatory)",
            self.repo_training_text or "(no repository training found at this sha)",
            "",
            "## FAILURE EVIDENCE (supplied verbatim)",
            "```",
            self.evidence.text.rstrip(),
            "```",
            "",
            f"## AIDER REPOMAP ({self.repomap.source})",
            "```",
            self.repomap.text.rstrip() or "(empty)",
            "```",
            "",
            "## RELEVANT SOURCE AT THIS SHA",
        ]
        for sl in self.source_slices:
            parts.append(f"### {sl.path}   ({sl.reason})")
            parts.append("```")
            parts.append(sl.text.rstrip())
            parts.append("```")
            parts.append("")
        parts.append("## REQUIRED OUTPUT")
        parts.append(
            "Reply with a single JSON object with keys: symptom, root_cause, "
            "affected_component, reasoning_summary, evidence (list of "
            "{kind, statement, file, line, symbol, quote}), confidence, uncertainty, "
            "recommended_next_action. Root cause must be a defect in this code at this "
            "sha, not the visible error. Every claim needs file/line evidence."
        )
        return "\n".join(parts)


# --------------------------------------------------------------------- builder
def resolve_frame_path(raw: str, files: list[str]) -> str | None:
    candidate = gitutil.normalise(raw)
    if candidate in files:
        return candidate
    lowered = candidate.lower()
    for known in files:
        if known.lower() == lowered:
            return known
    tail = "/" + lowered
    best: str | None = None
    for known in files:
        known_lower = known.lower()
        if known_lower.endswith(tail) and (best is None or len(known) < len(best)):
            best = known
    if best:
        return best
    for known in files:
        if known.lower().split("/")[-1] == lowered.split("/")[-1]:
            return known
    return None


def _classify(rel: str) -> str:
    parts = rel.split("/")
    if "tests" in parts or parts[-1].startswith("test_") or parts[-1].endswith("_test.py"):
        return "test"
    return "source"


def _slice(path: str, lines: list[str], anchors: list[int], budget: int) -> tuple[str, bool]:
    if not lines:
        return "", False
    if len(lines) * 60 <= budget:
        return "\n".join(lines), False
    keep: set[int] = set()
    for anchor in anchors:
        for offset in range(-18, 19):
            index = anchor + offset
            if 1 <= index <= len(lines):
                keep.add(index)
    if not keep:
        return "\n".join(lines[:40]), True
    out: list[str] = []
    previous = 0
    for index in sorted(keep):
        if previous and index > previous + 1:
            out.append("        ...")
        out.append(f"{index:>5}  {lines[index - 1]}")
        previous = index
    return "\n".join(out), True


def build_context(
    repo_path: Path,
    sha: str,
    evidence_path: str | Path,
    export_root: Path,
    map_tokens: int = 4096,
) -> AnalysisContext:
    files = gitutil.list_files(repo_path, sha)
    evidence = load_evidence(evidence_path)
    train = training_mod.load_training(export_root, repo_path, sha)
    repo_map = repomap_mod.build(export_root, files, map_tokens=map_tokens)
    index = SymbolIndex(export_root, files)

    resolved: list[dict] = []
    wanted: dict[str, set[int]] = {}
    reasons: dict[str, str] = {}
    for frame in evidence.frames:
        rel = resolve_frame_path(frame.file, files)
        entry = {
            "file": rel or frame.file,
            "line": frame.line,
            "function": frame.func,
            "quoted": frame.quoted,
            "kind": _classify(rel or frame.file),
            "resolved": rel is not None,
            "error": frame.error,
            "compact": frame.compact,
        }
        symbol = None
        if rel:
            info = index.function_at(rel, frame.line)
            if info:
                symbol = info.qualname
                entry["symbol"] = symbol
                entry["source_line"] = info.line_text(frame.line)
            wanted.setdefault(rel, set()).add(frame.line)
            reasons.setdefault(rel, "named in failure evidence")
        elif frame.line == 0:
            wanted.setdefault(frame.file, set()).add(0)
            reasons.setdefault(frame.file, "named in failure evidence")
        resolved.append(entry)

    slices: list[SourceSlice] = []
    budget = MAX_SOURCE_CHARS
    ranked = sorted(wanted.items(), key=lambda item: (item[0].startswith("tests/"), item[0]))
    for rel, anchors in ranked[:MAX_SOURCE_FILES]:
        lines = index.source_lines(rel)
        if not lines:
            text = gitutil.show_file(repo_path, sha, rel) or ""
            lines = text.splitlines()
        text, truncated = _slice(rel, lines, sorted(anchors), budget)
        budget -= len(text)
        slices.append(SourceSlice(path=rel, reason=reasons[rel], text=text, truncated=truncated))
        if budget <= 0:
            break

    commit = gitutil.commit_info(repo_path, sha).as_dict()
    return AnalysisContext(
        repo_path=repo_path,
        repo_name=repo_path.name,
        sha=sha,
        commit=commit,
        export_root=export_root,
        files=files,
        evidence=evidence,
        training=train,
        repomap=repo_map,
        index=index,
        source_slices=slices,
        resolved_frames=resolved,
        head_at_start=gitutil.head_sha(repo_path),
        dirty_at_start=bool(gitutil.status_porcelain(repo_path)),
        built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
