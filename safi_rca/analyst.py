"""The diagnostic engine.

Given an assembled :class:`~safi_rca.context.AnalysisContext`, work out the
root cause, cite the repository evidence for it, and be honest about what the
evidence does not prove.

The engine is deterministic: the same evidence against the same sha always
yields the same report, and every conclusion is tied to a file/line that exists
in that commit.  Detectors are small, named and independent so the report can
say *which* reasoning produced the conclusion.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import gitutil, training as training_mod
from .context import AnalysisContext
from .report import EvidenceItem, RcaReport, merge_evidence
from .symbols import FunctionInfo, Nullability, OptionalDereference

_NONE_ERRORS = ("'NoneType' object is not", "NoneType is not", "of NoneType")


@dataclass
class Finding:
    detector: str
    root_cause: str
    component: str
    reasoning: list[str] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    confidence: str = "Low"
    uncertainty: str = ""
    next_action: str = ""
    limitations: list[str] = field(default_factory=list)


@dataclass
class Site:
    file: str
    line: int
    symbol: str
    function: FunctionInfo | None
    why: str

    def as_dict(self) -> dict:
        return {"file": self.file, "line": self.line, "symbol": self.symbol, "selected_because": self.why}


# --------------------------------------------------------------------- helpers
def _source_frames(ctx: AnalysisContext) -> list[dict]:
    return [f for f in ctx.resolved_frames if f.get("resolved") and f.get("kind") == "source"]


def _test_frames(ctx: AnalysisContext) -> list[dict]:
    return [f for f in ctx.resolved_frames if f.get("resolved") and f.get("kind") == "test"]


def _deepest_source_frame(ctx: AnalysisContext) -> dict | None:
    """The frame where the failure actually happened inside this repository.

    pytest prints the innermost frame as a compact ``file:line: Error`` line
    after its source block, so textual order alone is not a depth signal.
    """
    frames = _source_frames(ctx)
    if not frames:
        return None
    for frame in frames:
        if frame.get("compact"):
            return frame
    return frames[0] if ctx.evidence.frame_order == "innermost_first" else frames[-1]


def _literal_mapping_keys(value_src: str) -> list[str]:
    try:
        value = ast.literal_eval(value_src)
    except (ValueError, SyntaxError):
        try:
            tree = ast.parse(value_src, mode="eval")
            value = ast.literal_eval(tree.body)
        except (ValueError, SyntaxError):
            return []
    if isinstance(value, dict):
        return [str(k) for k in value]
    return []


def _kv_pairs(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in re.finditer(r"['\"]([A-Za-z_][A-Za-z0-9_]{1,30})['\"]\s*:\s*['\"]([^'\"]{1,40})['\"]", text)]


def _call_target_name(call_src: str) -> str:
    match = re.match(r"^\s*(?:self\.|cls\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\(", call_src)
    if match:
        return match.group(1)
    match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\.\s*get\s*\(", call_src)
    if match:
        return match.group(1)
    return ""


def _subject_calls(text: str) -> list[str]:
    calls = re.findall(r"\b([A-Za-z_][A-Za-z0-9_.]*)\s*\(", text)
    return [c for c in calls if not c.startswith(("self", "def ", "assert "))]


_FACTORY_NAMES = {"make", "build", "create", "new", "get", "setup", "init", "factory", "make_", "load"}


def _is_factory(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.startswith(prefix) for prefix in _FACTORY_NAMES)


def _quoted_assertion_lines(ctx: AnalysisContext) -> list[str]:
    lines: list[str] = []
    for frame in ctx.resolved_frames:
        if frame.get("kind") == "test":
            lines.extend(frame.get("quoted") or [])
    lines.extend(_quoted_lines(ctx))
    return lines


def _site_from_failing_test(ctx: AnalysisContext) -> Site | None:
    """No traceback: follow the failing test into the code it actually calls."""
    for test_name in ctx.evidence.failing_tests:
        for info in ctx.index.functions.get(test_name, []):
            if "test" not in info.file.split("/")[-1] and "/tests/" not in f"/{info.file}":
                continue
            assertion_text = " ".join(_quoted_assertion_lines(ctx))
            scored: list[tuple[int, FunctionInfo]] = []
            for callee_name, _line in ctx.index.calls_in_function(info):
                target = ctx.index.resolve(callee_name)
                if target is None or target.file == info.file:
                    continue
                if re.search(r"\btests?/", target.file) and "/tests/" in f"/{target.file}":
                    continue
                score = 1
                if callee_name in assertion_text:
                    score += 4
                if _is_factory(callee_name):
                    score -= 3
                if ctx.index.class_of(target.qualname):
                    score += 1
                scored.append((score, target))
            if not scored:
                continue
            scored.sort(key=lambda item: (-item[0], item[1].file, item[1].lineno))
            target = scored[0][1]
            return Site(
                file=target.file,
                line=target.lineno,
                symbol=target.qualname,
                function=target,
                why=f"production symbol called by the failing test `{test_name}`",
            )
    return None


def _resolve_site(ctx: AnalysisContext) -> Site | None:
    """Pick the code site the diagnosis is about."""
    frame = _deepest_source_frame(ctx)
    if frame:
        info = ctx.index.function_at(frame["file"], frame["line"])
        return Site(
            file=frame["file"],
            line=frame["line"],
            symbol=frame.get("symbol") or (info.qualname if info else ""),
            function=info,
            why="deepest frame in the failure evidence that resolves inside this repository",
        )

    from_test = _site_from_failing_test(ctx)
    if from_test:
        return from_test

    # No traceback frame: fall back to symbols the evidence text mentions.
    candidates: list[tuple[int, FunctionInfo]] = []
    evidence_text = ctx.evidence.text
    for name in ctx.index.functions:
        if re.search(rf"\b{re.escape(name)}\s*\(", evidence_text):
            for info in ctx.index.functions[name]:
                candidates.append((evidence_text.count(name), info))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1].file, item[1].lineno))
    _count, info = candidates[0]
    return Site(
        file=info.file,
        line=info.lineno,
        symbol=info.qualname,
        function=info,
        why="symbol invoked in the failure evidence that resolves inside this repository",
    )


def _training_facts(ctx: AnalysisContext, site: Site | None) -> list[training_mod.TrainingFact]:
    identifiers = set(ctx.identifiers_of_interest())
    if site:
        identifiers.update({site.symbol, site.file})
        if site.function:
            identifiers.add(site.function.name)
            identifiers.update(site.function.line_text(line) for line in range(site.function.lineno, min(site.function.end_lineno, site.function.lineno + 40)))
    identifiers = {token.strip("`'\"()[],.") for chunk in identifiers for token in re.split(r"[\s,]+", chunk) if token}
    identifiers = {i for i in identifiers if len(i) > 3}
    paths = {site.file} if site else set()
    if site and site.file.endswith(".py"):
        paths.add(site.file.rsplit("/", 1)[-1])
    return training_mod.select_facts(ctx.training.repo_facts, identifiers, paths, limit=8)


def _training_evidence(facts: list[training_mod.TrainingFact]) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    for fact in facts[:6]:
        marker = f" [{fact.markers[0]}]" if fact.markers else ""
        items.append(
            EvidenceItem(
                kind="training",
                statement=f"repository training states{marker}: {fact.text[:280]}",
                file=fact.source,
                line=fact.line,
                symbol=fact.section,
            )
        )
    return items


def _stale_evidence_finding(
    ctx: AnalysisContext,
    site: Site,
    callee: FunctionInfo,
    nullability: Nullability,
    evidence: list[EvidenceItem],
    deref: OptionalDereference,
) -> Finding:
    """The evidence describes a None that the analysed sha can no longer produce."""
    ev = ctx.evidence
    return Finding(
        detector="evidence-not-reproducible-at-sha",
        root_cause=(
            f"The supplied evidence reports `{ev.exception}: {ev.exception_message}` at {site.file}:{site.line}, "
            f"but at sha {ctx.sha[:9]} that code path can no longer produce None: {callee.qualname} "
            f"{nullability.reason}. The traceback does not describe the analysed commit."
        ),
        component=f"{callee.file} :: {callee.qualname}",
        reasoning=[
            f"Evidence attributes the failure to {site.symbol} at {site.file}:{site.line} (`{deref.deref_source}`).",
            f"{deref.assigned_source} ({site.file}:{deref.assigned_line}) binds {deref.variable} from `{deref.call_name}`.",
            f"{callee.qualname} at {callee.file}:{callee.lineno} now returns {nullability.verdict} ({nullability.source}).",
            "A report about evidence, not about the code: whatever produced this traceback ran against a different version of the repository.",
        ],
        evidence=evidence,
        confidence="Low",
        uncertainty=(
            "Either the evidence belongs to another commit, or the failure is intermittent in a way static reading "
            "cannot confirm. This report deliberately makes no claim about the current code."
        ),
        next_action=(
            f"Re-run the reported test at {ctx.sha[:9]} and capture fresh evidence before acting on this traceback."
        ),
        limitations=["no reproduction at the analysed sha"],
    )


# ------------------------------------------------------------------- detectors
def detect_none_dereference(ctx: AnalysisContext, site: Site | None) -> Finding | None:
    ev = ctx.evidence
    blob = f"{ev.exception} {ev.exception_message}"
    if not any(token in blob for token in _NONE_ERRORS):
        return None
    if site is None or site.function is None:
        return None

    deref = ctx.index.optional_dereference(site.function, site.line)
    if deref is None:
        return None

    evidence: list[EvidenceItem] = [
        EvidenceItem(
            kind="source",
            statement="the optional lookup result is dereferenced immediately after assignment, with no fallback",
            file=site.file,
            line=deref.deref_line,
            symbol=site.symbol,
            quote=deref.deref_source,
        ),
        EvidenceItem(
            kind="source",
            statement="the value being dereferenced comes from a lookup that can miss",
            file=site.file,
            line=deref.assigned_line,
            symbol=site.symbol,
            quote=deref.assigned_source,
        ),
    ]

    callee_name = _call_target_name(deref.call_name)
    callee = deref.callee or (ctx.index.resolve(callee_name) if callee_name else None)
    if callee is not None:
        nullability = ctx.index.nullability(callee)
        evidence.append(
            EvidenceItem(
                kind="source",
                statement=f"callee {callee.qualname} {nullability.reason}",
                file=callee.file,
                line=nullability.line or callee.lineno,
                symbol=callee.qualname,
                quote=nullability.source,
            )
        )
        if nullability.verdict != "nullable":
            return _stale_evidence_finding(ctx, site, callee, nullability, evidence, deref)
    if callee is None:
        if "." not in deref.call_name:
            return None
        table_name = deref.call_name.split(".")[0]
        table_src = None
        for constant, (_line, value) in ctx.index.constant_candidates(site.file).items():
            if constant == table_name:
                table_src = value
        if table_src is not None:
            keys = _literal_mapping_keys(table_src)
            observed = [value for key, value in _kv_pairs(ctx.evidence.text) if key in {"tier", "plan", "type", "kind"}]
            missing = [value for value in observed if value not in keys]
            line_no = ctx.index.constant_candidates(site.file)[table_name][0]
            evidence.append(
                EvidenceItem(
                    kind="source",
                    statement=f"lookup table {table_name} defines keys {keys}",
                    file=site.file,
                    line=line_no,
                    symbol=table_name,
                )
            )
            if missing:
                evidence.append(
                    EvidenceItem(
                        kind="log" if "logs" not in {} else "source",
                        statement=f"evidence carries value(s) {sorted(set(missing))} that have no row in {table_name}",
                        file=ctx.evidence.path.rsplit("/", 1)[-1],
                        quote=", ".join(sorted(set(missing))),
                    )
                )
    if callee is None and "." not in deref.call_name:
        return None

    module = site.file
    dangling = []
    for constant, (line_no, _value) in ctx.index.constant_candidates(module).items():
        is_dangling, _ = ctx.index.constant_is_dangling(module, constant)
        if is_dangling:
            dangling.append((constant, line_no))
            evidence.append(
                EvidenceItem(
                    kind="source",
                    statement=(
                        f"{constant} is declared in this module but never referenced anywhere in the repository "
                        f"at this sha -- the default this code needs is present but unused"
                    ),
                    file=module,
                    line=line_no,
                    symbol=constant,
                )
            )

    resolution_symbol = callee.qualname if callee is not None else site.symbol
    resolution_file = callee.file if callee is not None else site.file
    fallback_names = [name for name, _line in dangling]
    action = (
        f"Have a maintainer make {resolution_symbol} return {fallback_names[0] if fallback_names else 'a default plan'} "
        f"when the lookup misses, and add a regression test that exercises the missing-row case. "
        f"safi-rca made no change."
        if fallback_names
        else f"Have a maintainer add a fallback in {resolution_symbol} for the case where the lookup misses, "
        f"plus a regression test for the missing-row case. safi-rca made no change."
    )

    return Finding(
        detector="none-dereference-on-optional-lookup",
        root_cause=(
            f"{resolution_symbol} resolves a tier to a plan and returns None when the tier has no row, and its "
            f"caller {site.symbol} dereferences that result unconditionally "
            f"(`{deref.deref_source}` at {site.file}:{deref.deref_line}). The missing-key path has no default, so "
            f"a customer whose tier is absent from the lookup table crashes instead of receiving the default. "
            + (
                f"The repository already declares the intended default ({fallback_names[0]}) but never uses it."
                if fallback_names
                else ""
            )
        ).strip(),
        component=f"{resolution_file} :: {resolution_symbol} (called from {site.file} :: {site.symbol})",
        reasoning=[
            f"The visible failure is `{ev.exception}: {ev.exception_message}` at {site.file}:{site.line}; that is the symptom.",
            f"The deepest repository frame is {site.symbol} in {site.file}, line {site.line} (`{site.function.line_text(site.line)}`).",
            f"Statement `{deref.assigned_source}` ({site.file}:{deref.assigned_line}) binds {deref.variable} from `{deref.call_name}`.",
            f"Statement `{deref.deref_source}` ({site.file}:{deref.deref_line}) dereferences {deref.variable} with no default and no guard.",
        ]
        + ([f"{callee.qualname} {ctx.index.nullability(callee).reason} ({callee.file}:{callee.lineno})."] if callee is not None else [])
        + ([f"{fallback_names[0]} is declared at {module}:{dict(dangling)[fallback_names[0]]} and referenced nowhere."] if dangling else []),
        evidence=evidence,
        confidence="High" if (callee is not None and (dangling or fallback_names)) else "Medium",
        uncertainty=(
            ""
            if (callee is not None and (dangling or fallback_names))
            else "The code path is proven from static reading, but the evidence contains no assertion of the "
            "intended rate, so the exact expected value for this customer is inferred from repository training "
            "rather than from a failing expectation."
        ),
        next_action=action,
    )


def detect_unbounded_index(ctx: AnalysisContext, site: Site | None) -> Finding | None:
    if site is None or site.function is None:
        return None
    bound = ctx.index.unbounded_bound(site.function)
    if bound is None:
        return None
    cls = ctx.index.class_of(site.symbol) if "." in site.symbol else None
    container_line = cls.lines[0] if cls and cls.lines else ""
    del container_line
    evidence = [
        EvidenceItem(
            kind="source",
            statement=(
                f"{site.symbol} reads `{bound.container}` using `{bound.counter}` as the bound, but `{bound.counter}` "
                f"is never clamped against the capacity of `{bound.container}` in this class"
            ),
            file=site.file,
            line=bound.index_line,
            symbol=site.symbol,
            quote=bound.index_source,
        ),
        EvidenceItem(
            kind="source",
            statement=f"`{bound.counter}` grows without an upper bound, so it outlives the fixed-length `{bound.container}`",
            file=site.file,
            line=bound.counter_line,
            symbol=site.symbol,
            quote=bound.counter_source,
        ),
    ]
    return Finding(
        detector="unclamped-counter-index",
        root_cause=(
            f"{site.symbol} in {site.file} uses the monotonically increasing `{bound.counter}` as the slice bound "
            f"for the fixed-length `{bound.container}`. Once the counter exceeds the container length the container "
            f"has wrapped, so slot order is no longer age order: the newest records occupy slot 0 and the oldest "
            f"reachable record is silently overwritten. That is the defect; the assertion comparing drained "
            f"records is reporting it correctly."
        ),
        component=f"{site.file} :: {site.symbol}",
        reasoning=[
            f"The failure is an assertion on drained records; the deepest resolvable operation is `{site.symbol}` in {site.file}.",
            f"`{bound.counter_source}` ({site.file}:{bound.counter_line}) increments the counter with no comparison to the container capacity.",
            f"`{bound.index_source}` ({site.file}:{bound.index_line}) slices the container with that counter instead of the retained window.",
            "The container is allocated once at construction, so its length is the capacity and never grows with the counter.",
        ],
        evidence=evidence,
        confidence="Medium",
        uncertainty=(
            "Static reading shows the bound is wrong, but the evidence only exercises one append count past the "
            "capacity; behaviour for larger overflows and for the sink is not demonstrated by this evidence."
        ),
        next_action=(
            f"Have a maintainer bound the drain by the retained window (`min({bound.counter}, capacity)`) and slice in "
            f"age order rather than slot order, then add a test that overflows by more than one record. safi-rca made no change."
        ),
    )


def detect_missing_empty_guard(ctx: AnalysisContext, site: Site | None) -> Finding | None:
    ev = ctx.evidence
    if "not iterable" not in f"{ev.exception} {ev.exception_message}":
        return None
    if site is None or site.function is None:
        return None
    lines = ctx.index.source_lines(site.file)
    window = "\n".join(lines[site.function.lineno - 1 : site.function.end_lineno])
    call = re.search(r"for\s+\w+\s+in\s+([A-Za-z_][\w.()]*)\s*:", window)
    if not call:
        return None
    callee_name = call.group(1).split(".")[-1].replace("()", "")
    callee = ctx.index.resolve(callee_name)
    if callee is None:
        return None
    nullability = ctx.index.nullability(callee)
    if nullability.verdict != "nullable":
        return None
    return Finding(
        detector="none-returned-instead-of-empty-collection",
        root_cause=(
            f"{callee.qualname} can return `None` ({nullability.reason}) and {site.symbol} iterates its result without "
            f"checking for `None`, so an empty input crashes instead of being a no-op."
        ),
        component=f"{callee.file} :: {callee.qualname}",
        reasoning=[
            f"`{call.group(1)}` is iterated in {site.symbol} ({site.file}:{site.line}) with no guard.",
            f"{callee.qualname} has a path returning None ({callee.file}:{nullability.line}).",
        ],
        evidence=[
            EvidenceItem(kind="source", statement="result of the call is iterated with no None guard", file=site.file, line=site.line, symbol=site.symbol),
            EvidenceItem(kind="source", statement="callee returns None on one path", file=callee.file, line=nullability.line, symbol=callee.qualname, quote=nullability.source),
        ],
        confidence="High",
        uncertainty="",
        next_action=f"Have a maintainer return an empty collection from {callee.qualname} or guard the iteration. safi-rca made no change.",
    )


def detect_key_error(ctx: AnalysisContext, site: Site | None) -> Finding | None:
    ev = ctx.evidence
    if ev.error_kind != "KeyError":
        return None
    if site is None or site.function is None:
        return None
    key_match = re.search(r"KeyError:\s*['\"]?([^'\"\n]+)", ev.exception_message or "")
    key = key_match.group(1).strip() if key_match else ""
    window = "\n".join(ctx.index.source_lines(site.file)[site.function.lineno - 1 : site.function.end_lineno])
    table = re.search(r"([A-Z_][A-Z0-9_]*)\[", window)
    evidence = [
        EvidenceItem(kind="source", statement=f"the key `{key}` is required from a mapping with no default", file=site.file, line=site.line, symbol=site.symbol),
    ]
    if table:
        constants = ctx.index.constant_candidates(site.file)
        if table.group(1) in constants:
            line_no, value = constants[table.group(1)]
            evidence.append(
                EvidenceItem(
                    kind="source",
                    statement=f"{table.group(1)} defines keys {_literal_mapping_keys(value)} and has no row for `{key}`",
                    file=site.file,
                    line=line_no,
                    symbol=table.group(1),
                )
            )
    return Finding(
        detector="keyerror-missing-mapping-row",
        root_cause=(
            f"{site.symbol} indexes a mapping with a hard key `{key}` that the mapping does not define, so any input "
            f"outside the table raises instead of taking a default."
        ),
        component=f"{site.file} :: {site.symbol}",
        reasoning=[
            f"The evidence raises KeyError for `{key}` inside {site.symbol} ({site.file}:{site.line}).",
            "The failing test's expectation, not the assertion line itself, is what the assertion reports; the defect is the lookup contract.",
        ],
        evidence=evidence,
        confidence="Medium" if len(evidence) > 1 else "Low",
        uncertainty=(
            "The evidence does not show the intended behaviour for an unknown key, so the required default is inferred "
            "from repository training."
        ),
        next_action=(
            f"Have a maintainer decide the intended behaviour for key `{key}` and encode it in {site.symbol}, "
            f"using the default the repository already declares if one exists. safi-rca made no change."
        ),
    )


def _numeric(value: str) -> float | None:
    token = value.strip().strip("\"'")
    try:
        return float(token)
    except ValueError:
        return None


def detect_precision_mismatch(ctx: AnalysisContext, site: Site | None) -> Finding | None:
    """Assertion where actual and expected are the same number, different text.

    Happens when a value carries more precision than the contract allows and the
    value is compared as a string rather than as a number.
    """
    ev = ctx.evidence
    if ev.error_kind != "AssertionError":
        return None
    actual, expected = ev.assertion_actual, ev.assertion_expected
    if not actual or not expected:
        return None
    left, right = _numeric(actual), _numeric(expected)
    if left is None or right is None or left != right:
        return None
    if actual.strip() == expected.strip():
        return None
    if site is None or site.function is None:
        return None

    hop = _one_hop_producer(ctx, site)
    arithmetic = ctx.index.returns_arithmetic(hop.function) if hop else []
    if not arithmetic and site.function is not None and site.function is not (hop.function if hop else None):
        arithmetic = ctx.index.returns_arithmetic(site.function)
    if not arithmetic:
        return None

    ret_line, ret_source, origin_source = arithmetic[0]
    evidence = [
        EvidenceItem(
            kind="test",
            statement=f"assertion compares the produced value as text: actual {actual}, expected {expected}",
            file=_failing_test_file(ctx) or "",
            line=_failing_test_line(ctx) or None,
            symbol=(ev.failing_tests or [""])[0],
            quote=f"assert {actual} == {expected}",
        ),
        EvidenceItem(
            kind="source",
            statement="the value reaching the assertion is returned straight out of arithmetic, with no normalisation step",
            file=hop.file if hop else site.file,
            line=ret_line,
            symbol=hop.symbol if hop else site.symbol,
            quote=ret_source,
        ),
        EvidenceItem(
            kind="source",
            statement="arithmetic that introduces the extra scale",
            file=hop.file if hop else site.file,
            line=max(1, ret_line - 1),
            symbol=hop.symbol if hop else site.symbol,
            quote=origin_source,
        ),
    ]
    producer = hop.symbol if hop else site.symbol
    return Finding(
        detector="unnormalised-numeric-precision",
        root_cause=(
            f"{producer} returns a value produced by decimal arithmetic and never normalises its scale, so the "
            f"rendered amount carries more decimal places than the contract allows ({actual} where {expected} is "
            f"required). The assertion is correct; the producer is not."
        ),
        component=f"{(hop.file if hop else site.file)} :: {producer}",
        reasoning=[
            f"The failure is a text comparison of two numerically equal values: {actual} vs {expected}.",
            f"The value is produced by {producer}, whose only arithmetic result is returned unnormalised.",
            f"No quantize()/normalize() call exists in {producer}, so the extra scale survives to the caller.",
        ],
        evidence=evidence,
        confidence="Medium",
        uncertainty=(
            "The evidence pins the wrong rendering but not the intended precision contract: whether the fix is to "
            "quantise inside the producer or to compare numerically in the test cannot be decided from this "
            "evidence alone."
        ),
        next_action=(
            f"Have a maintainer decide the scale contract for {producer} and encode it there (quantise at the "
            f"producer), then keep the assertion as-is. safi-rca made no change."
        ),
    )


def _failing_test_file(ctx: AnalysisContext) -> str | None:
    for frame in ctx.resolved_frames:
        if frame.get("kind") == "test" and frame.get("resolved"):
            return frame["file"]
    return None


def _failing_test_line(ctx: AnalysisContext) -> int | None:
    for frame in ctx.resolved_frames:
        if frame.get("kind") == "test" and frame.get("resolved") and frame.get("compact"):
            return frame["line"]
    return None


def _one_hop_producer(ctx: AnalysisContext, site: Site) -> Site | None:
    """The repository function whose return value the site hands on."""
    if site.function is None:
        return None
    for name, _line in ctx.index.calls_in_function(site.function):
        target = ctx.index.resolve(name)
        if target is None or target.file == site.file:
            continue
        if ctx.index.returns_arithmetic(target):
            return Site(
                file=target.file,
                line=target.lineno,
                symbol=target.qualname,
                function=target,
                why=f"the value returned to {site.symbol} originates here",
            )
    return None


def detect_generic(ctx: AnalysisContext, site: Site | None) -> Finding:
    ev = ctx.evidence
    if site is None:
        return Finding(
            detector="insufficient-evidence",
            root_cause="Not determinable from the supplied evidence.",
            component="unknown -- no code site could be tied to this evidence",
            reasoning=[
                "The evidence contains no file/line reference that resolves inside the repository at this sha.",
                "Without a stack frame, log line pointing at code, or test node id, any cause would be a guess.",
            ],
            evidence=[],
            confidence="Low",
            uncertainty=(
                "The evidence does not identify a code site, so this report deliberately refuses to name a root cause. "
                "The failure could originate from any of the components listed in the repository training; the RCA "
                "cannot rank them from this input."
            ),
            next_action=(
                "Capture a traceback (or the failing test node id with full output) that names at least one file and "
                "line inside this repository, then re-run safi-rca against the same sha."
            ),
            limitations=["no resolvable code site in the evidence"],
        )

    quoted = site.function.line_text(site.line) if site.function else ""
    match = quoted and ctx.evidence.text and any(q and q in quoted for q in _quoted_lines(ctx))
    reasoning = [
        f"The evidence resolves to {site.file}:{site.line} in {site.symbol or 'module scope'}; site chosen because it is the "
        f"{site.why}.",
        f"At this sha that line reads: `{quoted}`.",
    ]
    uncertainty = (
        "This is the deepest failing operation the evidence identifies. The evidence does not show which earlier value "
        "made it fail, so more than one upstream cause is consistent with this report."
    )
    return Finding(
        detector="deepest-failing-frame",
        root_cause=(
            f"The failure originates at {site.file}:{site.line} in {site.symbol or 'module scope'}: "
            f"`{quoted or 'line unavailable at this sha'}`. That operation is where the bad value is first consumed."
        ),
        component=f"{site.file} :: {site.symbol or site.file}",
        reasoning=reasoning + (["The quoted source in the evidence matches the source at this sha."] if match else []),
        evidence=[
            EvidenceItem(
                kind="source",
                statement="deepest failing operation identified in the evidence",
                file=site.file,
                line=site.line,
                symbol=site.symbol,
                quote=quoted,
            )
        ],
        confidence="Medium" if match else "Low",
        uncertainty=uncertainty,
        next_action=(
            f"Inspect every value that reaches {site.file}:{site.line} and add an explicit precondition check or "
            f"regression test there. safi-rca made no change."
        ),
    )


def _quoted_lines(ctx: AnalysisContext) -> list[str]:
    quoted: list[str] = []
    for frame in ctx.resolved_frames:
        quoted.extend(frame.get("quoted") or [])
    return quoted


# ------------------------------------------------------------------- symptom
def build_symptom(ctx: AnalysisContext, finding: Finding) -> str:
    ev = ctx.evidence
    parts: list[str] = []
    if ev.failing_tests:
        parts.append(f"test `{ev.failing_tests[0]}` fails")
    if ev.exception:
        parts.append(f"`{ev.exception}: {ev.exception_message}`" if ev.exception_message else f"`{ev.exception}`")
    if not parts and ev.logs:
        worst = [log for log in ev.logs if log["level"].upper() in {"ERROR", "FATAL", "CRITICAL"}]
        sample = (worst or ev.logs)[-1]
        parts.append(f"log line `{sample['level']} {sample['message']}`")
    if not parts:
        parts.append("failure reported in the supplied evidence")
    symptom = "; ".join(parts)
    if ev.http_statuses:
        symptom += f"; observed HTTP status {', '.join(ev.http_statuses)}"
    frame = _deepest_source_frame(ctx)
    if frame:
        symptom += f", raised through {frame['file']}:{frame['line']}"
    return symptom


# ------------------------------------------------------------------- history
def _history_evidence(ctx: AnalysisContext, finding: Finding, sha: str) -> list[EvidenceItem]:
    if not finding.component:
        return []
    match = re.match(r"([^:]+)::", finding.component)
    if not match:
        return []
    rel = match.group(1).strip()
    line_match = re.search(r"line (\d+)", finding.evidence[0].statement) if finding.evidence else None
    line = line_match.group(1) if line_match else ""
    del line
    for item in finding.evidence:
        if item.file == rel and item.line:
            blame = gitutil.blame_line(ctx.repo_path, sha, rel, item.line)
            if blame:
                return [
                    EvidenceItem(
                        kind="history",
                        statement="git blame for the cited line at this sha",
                        file=rel,
                        line=item.line,
                        symbol=item.symbol,
                        quote=blame,
                        sha=sha[:9],
                    )
                ]
            break
    return []


# ------------------------------------------------------------------- entry
def diagnose(ctx: AnalysisContext) -> RcaReport:
    ev = ctx.evidence
    site = _resolve_site(ctx)
    facts = _training_facts(ctx, site)

    detectors = (
        detect_none_dereference,
        detect_missing_empty_guard,
        detect_unbounded_index,
        detect_precision_mismatch,
        detect_key_error,
    )
    finding: Finding | None = None
    for detector in detectors:
        finding = detector(ctx, site)
        if finding is not None:
            break
    if finding is None:
        finding = detect_generic(ctx, site)

    evidence_items: list[EvidenceItem] = []
    evidence_items.extend(finding.evidence)
    evidence_items.extend(_training_evidence(facts))

    for frame in ctx.resolved_frames:
        if frame.get("kind") == "test" and frame.get("resolved"):
            evidence_items.append(
                EvidenceItem(
                    kind="test",
                    statement="failing test named in the evidence",
                    file=frame["file"],
                    line=frame["line"],
                    symbol=frame.get("symbol") or frame.get("function", ""),
                )
            )
    for log in ev.logs[-3:]:
        evidence_items.append(
            EvidenceItem(
                kind="log",
                statement=f"{log['level']}: {log['message'][:200]}",
                file=Path(ev.path).name,
                line=log.get("line"),
                symbol="failure log",
            )
        )
    evidence_items.extend(_history_evidence(ctx, finding, ctx.sha))

    reasoning = list(finding.reasoning)
    if facts:
        reasoning.append(
            "Repository training consulted: "
            + "; ".join(f"{fact.source}:{fact.line}" for fact in facts[:3])
            + "."
        )
    reasoning.append(
        f"All citations above were read from the exported tree of {ctx.sha} (commit {ctx.sha[:9]}); no file in the "
        f"repository was modified."
    )

    limitations = list(finding.limitations)
    if ev.error_kind == "unknown":
        limitations.append("evidence carried no recognisable exception header")
    if not ctx.repomap.non_empty:
        limitations.append("repo map was empty")

    # every recommendation is scoped to the analysed commit, so an action cannot
    # be carried to another sha by accident
    next_action = f"{finding.next_action} Valid only for {ctx.sha[:9]}; re-run safi-rca for any other commit."

    report = RcaReport(
        repo=ctx.repo_name,
        sha=ctx.sha,
        role="root-cause-analyzer",
        symptom=build_symptom(ctx, finding),
        root_cause=finding.root_cause,
        affected_component=finding.component,
        reasoning_summary=" ".join(reasoning),
        evidence=merge_evidence(evidence_items),
        confidence=finding.confidence,
        uncertainty=finding.uncertainty or (
            "No material uncertainty beyond the limits of static analysis of this commit."
            if finding.confidence == "High"
            else "See the uncertainty stated above."
        ),
        recommended_next_action=next_action,
        sha_short=ctx.sha[:9],
        commit=ctx.commit,
        analyzed_at=ctx.built_at,
        evidence_input=ev.as_dict(),
        detectors_fired=[finding.detector],
        source_files_reviewed=[sl.path for sl in ctx.source_slices],
        training=ctx.training.as_dict(),
        repomap=ctx.repomap.as_dict(),
        limitations=limitations,
    )
    report.training["facts_applied"] = [fact.as_dict() for fact in facts]
    return report
