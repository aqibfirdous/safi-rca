"""Compatibility shim: let Aider's RepoMap work with tree-sitter >= 0.22.

Aider 0.16 calls ``Language.query(src)``; tree-sitter 0.22 removed that method
in favour of ``tree_sitter.Query(language, src)`` and changed ``Query.captures``
to return a dict.  Neither Aider nor the repository is modified -- the missing
methods are re-attached to the tree-sitter classes at import time so that
Aider's own tag extraction runs unmodified.
"""

from __future__ import annotations

_APPLIED = False


def install() -> bool:
    """Attach ``Language.query`` when the installed tree-sitter lacks it."""
    global _APPLIED
    if _APPLIED:
        return True
    try:
        import tree_sitter  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - tree-sitter is optional
        return False

    language_cls = getattr(tree_sitter, "Language", None)
    query_cls = getattr(tree_sitter, "Query", None)
    if language_cls is None or query_cls is None or hasattr(language_cls, "query"):
        _APPLIED = True
        return True

    cursor_cls = getattr(tree_sitter, "QueryCursor", None)

    class _Captures(list):
        """Expose the Aider-era ``captures(node) -> [(node, tag)]`` API."""

        def __init__(self, language, source):
            super().__init__()
            self.language = language
            self.source = source
            self._query = query_cls(language, source)

        def captures(self, node):  # type: ignore[no-untyped-def]
            if cursor_cls is not None:
                raw = cursor_cls(self._query).captures(node)
            else:  # pragma: no cover - older tree-sitter
                raw = self._query.captures(node)
            if isinstance(raw, dict):
                flat: list[tuple] = []
                for tag, nodes in raw.items():
                    flat.extend((item, tag) for item in nodes)
                return flat
            return list(raw)

    def _query(self, source):  # type: ignore[no-untyped-def]
        return _Captures(self, source)

    language_cls.query = _query  # type: ignore[attr-defined]
    _APPLIED = True
    return True