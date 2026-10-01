"""Failure evidence parsing.

Accepts pytest output, plain Python tracebacks, build logs, HTTP error dumps
and free-form log excerpts.  Nothing here interprets the failure -- it only
turns bytes into structured facts that the diagnostic engine can reason about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .gitutil import normalise

_PYTEST_FRAME = re.compile(r"^(?P<file>[^\s:][^:]*?):(?P<line>\d+):(?:\s+in\s+(?P<func>\S+))?\s*$")
_PYTEST_LOCATION = re.compile(r"^(?P<file>[^\s:][^:]*?):(?P<line>\d+):\s+(?P<what>.+?)\s*$")
_PYTEST_ID = re.compile(r"^(?:FAILED|ERROR)\s+(?P<file>\S+?\.py)(?:::(?P<test>\S+))?")
_PYTEST_TITLE = re.compile(r"^_{3,}\s*(?P<test>test_[^\s]+|[A-Za-z_][\w\[\], ]*)\s*_{3,}")
_PYTEST_SHORT = re.compile(r"^(?P<file>[^\s:]+\.py):(?P<line>\d+):\s+(?P<err>[A-Za-z_]*(?:Error|Exception|Failed|Timeout).*)$")
_STDLIB_FRAME = re.compile(r'^File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.+)$')
_STDLIB_CAUSE = re.compile(r"^(?P<err>[A-Za-z_]*(?:Error|Exception|Interrupt|Warning)): ?(?P<msg>.*)$")
_ERROR_LINE = re.compile(r"^E\s+(?P<text>.+)$")
_CARET_BLOCK = re.compile(r"^>\s+(?P<text>.+)$")
_HTTP_STATUSES = {"400", "401", "403", "404", "409", "422", "500", "502", "503", "504"}

_LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?Z?)\s+"
    r"(?P<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\b\s*(?P<message>.*)$"
)
_HTTP_LINE = re.compile(r"\b(?P<status>\d{3})\b\s*(?P<reason>[A-Za-z ]+)?")
_ASSERT_DIFF_INDEX = re.compile(r"^At index (?P<index>\d+) diff: (?P<actual>.+?) != (?P<expected>.+)$")
_ASSERTION_PREFIX = re.compile(r"^(?:AssertionError:\s*)?(?:\.\.\.\s*(?:- \d+ > \d+ =)?\s*)?")
_ASSERT_EQ = re.compile(r"^assert (?P<actual>.+?)(?:\s*==\s*(?P<expected>.+))?$")
_ASSERT_ALPHA = re.compile(r"^(?P<left>.+)\s+(?:not in|in)\s+(?P<right>.+)$")

ERROR_TYPES = (
    "TypeError",
    "ValueError",
    "KeyError",
    "AttributeError",
    "IndexError",
    "ZeroDivisionError",
    "RuntimeError",
    "ImportError",
    "ModuleNotFoundError",
    "AssertionError",
    "NameError",
    "ArithmeticError",
    "LookupError",
    "Exception",
)


@dataclass
class EvidenceFrame:
    file: str
    line: int
    func: str = ""
    quoted: list[str] = field(default_factory=list)
    kind: str = "traceback"          # traceback | log | http
    error: str = ""
    compact: bool = False            # pytest's `file:line: Error` innermost frame

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" + (f" in {self.func}" if self.func else "")

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "function": self.func,
            "kind": self.kind,
            "error": self.error,
            "compact": self.compact,
            "quoted_source": self.quoted,
        }


@dataclass
class FailureEvidence:
    path: str
    text: str
    frames: list[EvidenceFrame] = field(default_factory=list)
    exception: str = ""
    exception_message: str = ""
    failing_tests: list[str] = field(default_factory=list)
    assertion_actual: str = ""
    assertion_expected: str = ""
    logs: list[dict] = field(default_factory=list)
    http_statuses: list[str] = field(default_factory=list)
    frame_order: str = "outermost_first"

    @property
    def error_kind(self) -> str:
        blob = f"{self.exception} {self.exception_message}"
        for kind in ERROR_TYPES:
            if kind in blob:
                return kind
        return "unknown"

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "bytes": len(self.text),
            "exception": self.exception,
            "error_kind": self.error_kind,
            "failing_tests": self.failing_tests,
            "frames": [f.as_dict() for f in self.frames],
            "log_lines": len(self.logs),
            "http_statuses": self.http_statuses,
        }


def load_evidence(path: str | Path) -> FailureEvidence:
    evidence_path = Path(path).expanduser().resolve()
    if not evidence_path.is_file():
        raise FileNotFoundError(f"evidence file not found: {evidence_path}")
    text = evidence_path.read_text(encoding="utf-8", errors="replace")
    return parse_evidence(text, source=str(evidence_path))


def parse_evidence(text: str, source: str = "<evidence>") -> FailureEvidence:
    ev = FailureEvidence(path=source, text=text)
    lines = text.splitlines()

    exception_lines: list[str] = []
    assertion_actual = assertion_expected = ""
    pending: list[EvidenceFrame] = []
    quoted: list[str] = []
    seen_locations: set[tuple[str, int]] = set()

    def new_frame(file: str, line: int, func: str = "", compact: bool = False) -> EvidenceFrame:
        """Attach any pending `>` source block to the frame it belongs to.

        pytest prints the quoted source *before* the `file:line` reference, so
        the block must be flushed forward, not backward.
        """
        nonlocal quoted
        frame = EvidenceFrame(file=file, line=line, func=func, compact=compact)
        if quoted:
            frame.quoted.extend(quoted)
            quoted = []
        pending.append(frame)
        return frame

    for line_no, raw in enumerate(lines, start=1):
        line = raw.rstrip()
        stripped = line.strip()

        title = _PYTEST_TITLE.match(stripped)
        if title and title.group("test") not in ev.failing_tests:
            ev.failing_tests.append(title.group("test"))

        ident = _PYTEST_ID.match(stripped)
        if ident:
            test = ident.group("test") or ident.group("file")
            if test not in ev.failing_tests:
                ev.failing_tests.append(test)

        stdlib = _STDLIB_FRAME.match(stripped)
        if stdlib:
            new_frame(
                file=normalise(stdlib.group("file")),
                line=int(stdlib.group("line")),
                func=stdlib.group("func").strip(),
            )
            continue

        if stripped and set(stripped) <= {"_"}:
            continue

        caret = _CARET_BLOCK.match(line)
        if caret:
            quoted.append(caret.group("text").strip())
            continue

        err_line = _ERROR_LINE.match(line)
        if err_line:
            body = err_line.group("text").strip()
            exception_lines.append(body)
            # `E   AssertionError: assert '1.000' == '1.00'` carries the actual
            # and expected values; strip the prefix before matching.
            assertion_body = body
            prefix = _ASSERTION_PREFIX.match(assertion_body)
            if prefix:
                assertion_body = assertion_body[prefix.end() :].strip()
            diff = _ASSERT_DIFF_INDEX.match(assertion_body)
            if diff:
                assertion_actual = diff.group("actual").strip()
                assertion_expected = diff.group("expected").strip()
            else:
                eq = _ASSERT_EQ.match(assertion_body)
                if eq:
                    assertion_actual = (eq.group("actual") or "").strip()
                    assertion_expected = (eq.group("expected") or "").strip()
                else:
                    alpha = _ASSERT_ALPHA.match(assertion_body)
                    if alpha:
                        assertion_actual = alpha.group("left").strip()
                        assertion_expected = alpha.group("right").strip()
            continue

        short = _PYTEST_SHORT.match(stripped)
        if short:
            exception_lines.append(short.group("err"))
            # `src/payment/validator.py:32: TypeError` is pytest's compact form
            # of the innermost frame -- it must not be dropped.
            if "/" in normalise(short.group("file")) or "\\" in short.group("file"):
                frame = new_frame(
                    file=normalise(short.group("file")),
                    line=int(short.group("line")),
                    compact=True,
                )
                frame.error = short.group("err")
            continue

        frame_match = _PYTEST_FRAME.match(stripped)
        if frame_match and frame_match.group("file"):
            candidate = normalise(frame_match.group("file"))
            if "/" in candidate or candidate.endswith((".py", ".js", ".ts", ".java", ".go", ".rb")):
                new_frame(candidate, int(frame_match.group("line")), frame_match.group("func") or "")
                continue

        location = _PYTEST_LOCATION.match(stripped)
        if location and location.group("file"):
            candidate = normalise(location.group("file"))
            if "/" in candidate:
                new_frame(candidate, int(location.group("line")), location.group("what"))
                continue

        log = _LOG_LINE.match(stripped)
        if log:
            ev.logs.append(
                {
                    "ts": log.group("ts"),
                    "level": log.group("level"),
                    "message": log.group("message").strip(),
                    "line": line_no,
                }
            )

        for status in _HTTP_LINE.findall(line):
            if str(status) in _HTTP_STATUSES:
                token = str(status)
                if token not in ev.http_statuses:
                    ev.http_statuses.append(token)

    if quoted and pending:
        pending[-1].quoted.extend(quoted)

    for frame in pending:
        key = (frame.file, frame.line)
        if key in seen_locations:
            continue
        seen_locations.add(key)
        ev.frames.append(frame)

    _pick_exception(ev, exception_lines, lines)
    ev.assertion_actual = assertion_actual
    ev.assertion_expected = assertion_expected
    ev.frame_order = detect_frame_order(lines)
    return ev


def detect_frame_order(lines: list[str]) -> str:
    """pytest prints innermost frame first, a plain traceback prints it last."""
    pytest_markers = (
        "short test summary info",
        "_ _ _ _ _",
        "=== FAILURES ===",
        "=== ERRORS ===",
    )
    for line in lines:
        stripped = line.strip()
        if any(marker in stripped for marker in pytest_markers):
            return "innermost_first"
        if _PYTEST_TITLE.match(stripped):
            return "innermost_first"
    for line in lines:
        if line.startswith("Traceback (most recent call last)"):
            return "outermost_first"
    return "outermost_first"


def _pick_exception(ev: FailureEvidence, error_lines: list[str], all_lines: list[str]) -> None:
    for body in error_lines:
        cause = _STDLIB_CAUSE.match(body.strip())
        if cause:
            ev.exception = cause.group("err")
            ev.exception_message = cause.group("msg").strip()
            return
        if "AssertionError" in body:
            ev.exception = "AssertionError"
            ev.exception_message = body.replace("AssertionError", "").strip(" :")
            return
    if not ev.exception:
        for line in reversed(all_lines):
            stripped = line.strip()
            cause = _STDLIB_CAUSE.match(stripped)
            if cause and cause.group("err") in ERROR_TYPES:
                ev.exception = cause.group("err")
                ev.exception_message = cause.group("msg").strip()
                return
    if ev.exception == "AssertionError" and ev.assertion_actual:
        ev.exception_message = (
            f"actual {ev.assertion_actual} != expected {ev.assertion_expected}"
        ).strip()
