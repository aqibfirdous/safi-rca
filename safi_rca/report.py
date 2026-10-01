"""The structured RCA report: JSON contract plus human-readable rendering."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

CONFIDENCE_ORDER = {"High": 3, "Medium": 2, "Low": 1}


@dataclass
class EvidenceItem:
    kind: str          # source | test | log | history | repomap | training | runtime
    statement: str
    file: str = ""
    line: int | None = None
    symbol: str = ""
    quote: str = ""
    sha: str = ""

    def as_dict(self) -> dict:
        data = {"kind": self.kind, "statement": self.statement}
        for key, value in (("file", self.file), ("line", self.line), ("symbol", self.symbol), ("quote", self.quote), ("sha", self.sha)):
            if value not in ("", None):
                data[key] = value
        return data


@dataclass
class RcaReport:
    repo: str
    sha: str
    role: str
    symptom: str
    root_cause: str
    affected_component: str
    reasoning_summary: str
    evidence: list[EvidenceItem] = field(default_factory=list)
    confidence: str = "Low"
    uncertainty: str = ""
    recommended_next_action: str = ""
    sha_short: str = ""
    commit: dict = field(default_factory=dict)
    analyzed_at: str = ""
    evidence_input: dict = field(default_factory=dict)
    detectors_fired: list[str] = field(default_factory=list)
    source_files_reviewed: list[str] = field(default_factory=list)
    training: dict = field(default_factory=dict)
    repomap: dict = field(default_factory=dict)
    runtime: dict = field(default_factory=dict)
    read_only: dict = field(default_factory=dict)
    sha_protection: dict = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = {
            "repo": self.repo,
            "sha": self.sha,
            "sha_short": self.sha_short or self.sha[:9],
            "role": self.role,
            "symptom": self.symptom,
            "root_cause": self.root_cause,
            "affected_component": self.affected_component,
            "reasoning_summary": self.reasoning_summary,
            "evidence": [item.as_dict() for item in self.evidence],
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "recommended_next_action": self.recommended_next_action,
            "commit": self.commit,
            "analyzed_at": self.analyzed_at,
            "evidence_input": self.evidence_input,
            "detectors_fired": self.detectors_fired,
            "source_files_reviewed": self.source_files_reviewed,
            "training": self.training,
            "repomap": self.repomap,
            "runtime": self.runtime,
            "read_only": self.read_only,
            "sha_protection": self.sha_protection,
            "limitations": self.limitations,
        }
        return data

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False)

    def render(self) -> str:
        lines = [
            "=" * 78,
            f"ROOT CAUSE REPORT  --  {self.repo}",
            "=" * 78,
            f"repo                : {self.repo}",
            f"sha (analysed)      : {self.sha}",
            f"sha (short)         : {self.sha_short or self.sha[:9]}",
            f"commit subject      : {self.commit.get('subject', '')}",
            f"role                : {self.role}",
            f"analyzed at         : {self.analyzed_at}",
            "",
            "-" * 78,
            "SYMPTOM",
            "-" * 78,
            self.symptom,
            "",
            "-" * 78,
            "ROOT CAUSE",
            "-" * 78,
            self.root_cause,
            "",
            "-" * 78,
            "AFFECTED COMPONENT",
            "-" * 78,
            self.affected_component,
            "",
            "-" * 78,
            "REASONING SUMMARY",
            "-" * 78,
            self.reasoning_summary,
            "",
            "-" * 78,
            "EVIDENCE",
            "-" * 78,
        ]
        if self.evidence:
            for index, item in enumerate(self.evidence, start=1):
                location = ""
                if item.file:
                    location = f"{item.file}:{item.line}" if item.line else item.file
                head = f"{index}. [{item.kind}]"
                if location:
                    head += f" {location}"
                if item.symbol:
                    head += f" ({item.symbol})"
                lines.append(head)
                lines.append(f"   {item.statement}")
                if item.quote:
                    lines.append(f"   | {item.quote}")
        else:
            lines.append("(none -- see UNCERTAINTY)")
        lines += [
            "",
            "-" * 78,
            "CONFIDENCE / UNCERTAINTY",
            "-" * 78,
            f"Confidence: {self.confidence}",
            f"Remaining uncertainty: {self.uncertainty or 'none stated'}",
            "",
            "-" * 78,
            "RECOMMENDED NEXT ACTION",
            "-" * 78,
            self.recommended_next_action,
            "",
            "-" * 78,
            "PROVENANCE",
            "-" * 78,
            f"repo training       : {', '.join(d.get('path', '?') for d in self.training.get('repo_training', [])) or 'none found'}",
            f"role training       : {self.training.get('role_training', {}).get('origin', 'unknown')}",
            f"repomap             : {self.repomap.get('producer', 'n/a')} "
            f"({self.repomap.get('files_mapped', 0)} files, {self.repomap.get('chars', 0)} chars)",
            f"runtime             : {self.runtime.get('selected', 'n/a')}",
            f"detectors fired     : {', '.join(self.detectors_fired) or 'none'}",
            f"read-only verified  : {self.read_only.get('clean', False)} "
            f"(HEAD {self.read_only.get('head_before', '?')} -> {self.read_only.get('head_after', '?')})",
        ]
        if self.limitations:
            lines += ["", "LIMITATIONS", "-" * 78, *[f"- {item}" for item in self.limitations]]
        lines.append("=" * 78)
        return "\n".join(lines)


def report_from_dict(data: dict) -> RcaReport:
    report = RcaReport(
        repo=data.get("repo", ""),
        sha=data.get("sha", ""),
        role=data.get("role", "root-cause-analyzer"),
        symptom=data.get("symptom", ""),
        root_cause=data.get("root_cause", ""),
        affected_component=data.get("affected_component", ""),
        reasoning_summary=data.get("reasoning_summary", ""),
        confidence=data.get("confidence", "Low"),
        uncertainty=data.get("uncertainty", ""),
        recommended_next_action=data.get("recommended_next_action", ""),
        sha_short=data.get("sha_short", ""),
        commit=data.get("commit", {}),
        analyzed_at=data.get("analyzed_at", ""),
        evidence_input=data.get("evidence_input", {}),
        detectors_fired=data.get("detectors_fired", []),
        source_files_reviewed=data.get("source_files_reviewed", []),
        training=data.get("training", {}),
        repomap=data.get("repomap", {}),
        runtime=data.get("runtime", {}),
        read_only=data.get("read_only", {}),
        sha_protection=data.get("sha_protection", {}),
        limitations=data.get("limitations", []),
    )
    report.evidence = [EvidenceItem(**item) for item in data.get("evidence", [])]
    return report


def lower_confidence(current: str, floor: str) -> str:
    if CONFIDENCE_ORDER.get(current, 0) > CONFIDENCE_ORDER.get(floor, 0):
        return floor
    return current


def raise_confidence(current: str, ceiling: str) -> str:
    if CONFIDENCE_ORDER.get(current, 0) < CONFIDENCE_ORDER.get(ceiling, 0):
        return ceiling
    return current


def merge_evidence(*groups: list[EvidenceItem]) -> list[EvidenceItem]:
    seen: set[tuple] = set()
    merged: list[EvidenceItem] = []
    for group in groups:
        for item in group:
            key = (item.kind, item.file, item.line, item.statement)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def as_jsonable(value) -> dict:
    return asdict(value)
