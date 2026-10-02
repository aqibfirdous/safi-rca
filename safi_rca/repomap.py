"""Repository structure via Aider RepoMap.

Primary path is Aider itself: ``aider.repomap.RepoMap`` is Aider's supported
Python interface and produces the exact RepoMap text Aider puts in context (a
file tree plus ranked, reference-counted signatures).  We never reach into
Aider internals beyond that class.

It runs against the *exported* copy of the analysed commit, so the tags cache
Aider writes lands in a scratch directory and not in the user's repository.

If Aider cannot be used (not installed, no tokenizer, unreadable tree) an
equivalent AST-derived RepoMap in the same format is produced instead and the
report records which producer was used.
"""

from __future__ import annotations

import ast
import io
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from .gitutil import is_text_candidate

_SKIP_DIR_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".aider.tags.cache.v1"}


@dataclass
class RepoMapResult:
    text: str
    source: str
    files_considered: int
    files_mapped: int = 0
    error: str | None = None
    identifiers: list[str] = field(default_factory=list)

    @property
    def non_empty(self) -> bool:
        return bool(self.text.strip())

    def as_dict(self) -> dict:
        return {
            "producer": self.source,
            "non_empty": self.non_empty,
            "files_considered": self.files_considered,
            "files_mapped": self.files_mapped,
            "chars": len(self.text),
            "error": self.error,
        }


def _eligible(export_root: Path, rel: str) -> bool:
    parts = rel.split("/")
    if any(part in _SKIP_DIR_PARTS for part in parts[:-1]):
        return False
    if parts[-1].startswith(".aider.tags.cache"):
        return False
    return is_text_candidate(rel)


def _aider_version() -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import aider  # noqa: PLC0415

            return getattr(aider, "__version__", "unknown")
    except Exception:  # pragma: no cover - environment dependent
        return "unavailable"


def _close_aider_cache(repo_map) -> None:
    """Release Aider's tags cache.

    Aider keeps the RepoMap in a ``diskcache.Cache``, i.e. an open SQLite handle.
    Leaving it open holds a file lock on Windows, which makes the export
    directory undeletable -- so a run would fail during its own cleanup rather
    than at the point of the mistake.
    """

    cache = getattr(repo_map, "TAGS_CACHE", None)
    close = getattr(cache, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - closing a cache must never fail a run
            pass


def build_with_aider(export_root: Path, files: list[str], map_tokens: int) -> RepoMapResult:
    from . import openai_compat, tree_sitter_compat  # noqa: PLC0415

    tree_sitter_compat.install()
    openai_compat.install()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from aider.io import InputOutput  # noqa: PLC0415
        from aider.repomap import RepoMap  # noqa: PLC0415

    ranked = [str(export_root / rel) for rel in files if _eligible(export_root, rel)]
    repo_map = RepoMap(map_tokens=map_tokens, root=str(export_root), io=InputOutput(), verbose=False)
    try:
        text = repo_map.get_repo_map(chat_files=[], other_files=ranked) or ""
    finally:
        _close_aider_cache(repo_map)
    headers = {line.rstrip().split(":")[0] for line in text.splitlines() if line.rstrip().endswith(":")}
    mapped = min(len(headers), len(ranked))
    return RepoMapResult(
        text=text,
        source=f"aider {_aider_version()} (aider.repomap.RepoMap)",
        files_considered=len(files),
        files_mapped=mapped,
    )


def _signature(node: ast.AST) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = ", ".join(a.arg for a in node.args.args)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({args})"
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(
            b.id if isinstance(b, ast.Name) else ast.unparse(b) for b in node.bases
        )
        return f"class {node.name}({bases})" if bases else f"class {node.name}"
    return None


def _file_signatures(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return []
    lines: list[str] = []
    for node in tree.body:
        sig = _signature(node)
        if not sig:
            continue
        if isinstance(node, ast.ClassDef):
            lines.append(f"  {sig}")
            for sub in node.body:
                sub_sig = _signature(sub)
                if sub_sig:
                    lines.append(f"    | {sub_sig}")
        else:
            lines.append(f"  | {sig}")
    return lines


def build_with_ast(export_root: Path, files: list[str], map_tokens: int, reason: str) -> RepoMapResult:
    eligible = [rel for rel in files if _eligible(export_root, rel) and rel.endswith(".py")]
    lines: list[str] = []
    mapped = 0
    for rel in eligible:
        sigs = _file_signatures(export_root / rel)
        if not sigs:
            continue
        mapped += 1
        lines.append(f"{rel}:")
        lines.extend(sigs)
    budget = max(1024, map_tokens * 4)
    text = "\n".join(lines)
    if len(text) > budget:
        text = text[:budget]
    return RepoMapResult(
        text=text,
        source="ast-fallback (aider unavailable)",
        files_considered=len(files),
        files_mapped=mapped,
        error=reason,
    )


def build(export_root: Path, files: list[str], map_tokens: int = 4096) -> RepoMapResult:
    """Return a RepoMap for the exported tree, preferring Aider."""
    try:
        result = build_with_aider(export_root, files, map_tokens)
        if result.non_empty:
            result.identifiers = sorted(set(_identifier_tokens(result.text)))
            return result
        return build_with_ast(export_root, files, map_tokens, "aider returned an empty repo map")
    except Exception as exc:  # noqa: BLE001 - any aider failure must degrade, not crash
        return build_with_ast(export_root, files, map_tokens, f"{type(exc).__name__}: {exc}")


def _identifier_tokens(text: str) -> list[str]:
    import re

    return [t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text) if len(t) > 3]


def repo_map_identifiers(result: RepoMapResult) -> set[str]:
    return set(result.identifiers)
