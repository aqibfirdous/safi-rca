"""Read credentials from a ``.env`` file, with no third-party dependency.

Two rules make this safe rather than merely convenient:

* ``.env`` is gitignored and never committed; only ``.env.example`` is.
* Values already present in the real environment win, so an explicit
  ``set GEMINI_API_KEY=...`` overrides the file. A ``.env`` can never silently
  shadow the credential an operator just set on purpose.

``python-dotenv`` would do this too, but this repository already juggles a pinned
``aider-chat`` against ``openhands-sdk``; adding another dependency to save
thirty lines is a bad trade. Loading is done once, lazily, and failures are
ignored -- a malformed ``.env`` must never stop a run that has valid credentials.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Searched in order; the first readable file wins.
CANDIDATES = (".env",)

_loaded = False


def _parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_dotenv(*, start: Path | None = None, force: bool = False) -> dict[str, str]:
    """Load ``.env`` into the environment once.  Returns the values applied."""

    global _loaded
    if _loaded and not force:
        return {}

    # An escape hatch so a test run or CI job can never pick up a real
    # credential from the developer's working copy, and start a live model call.
    if os.environ.get("SAFI_RCA_NO_DOTENV", "").strip().lower() in {"1", "true", "yes"}:
        _loaded = True
        return {}

    bases = [start] if start is not None else [Path.cwd()]
    bases.append(Path(__file__).resolve().parent.parent)
    found: dict[str, str] = {}
    for base in bases:
        for name in CANDIDATES:
            path = base / name
            try:
                if path.is_file():
                    found = _parse(path.read_text(encoding="utf-8", errors="replace"))
                    break
            except OSError:
                continue
        if found:
            break

    _loaded = True
    for key, value in found.items():
        os.environ.setdefault(key, value)
    return found


def redact(value: str | None) -> str:
    """Render a credential for display without revealing it."""

    if not value:
        return ""
    text = str(value)
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-2:]}" + f" ({len(text)} chars)"