"""A small AST index over the exported tree.

Only Python is indexed -- the analyzer reads a handful of files, so a targeted
index beats a full language server.  The index answers the questions the
diagnostic engine actually asks: where is this symbol defined, what does this
function return, is a value dereferenced without a fallback, is an index bound
unbounded.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FunctionInfo:
    name: str
    qualname: str
    file: str
    lineno: int
    end_lineno: int
    cls: str | None
    node: ast.AST = field(repr=False, default=None)
    source: list[str] = field(default_factory=list)

    def line_text(self, line: int) -> str:
        if 1 <= line <= len(self.source):
            return self.source[line - 1].strip()
        return ""

    def as_dict(self) -> dict:
        return {"symbol": self.qualname, "file": self.file, "line": self.lineno}


@dataclass
class ClassInfo:
    name: str
    file: str
    lineno: int
    methods: dict[str, FunctionInfo] = field(default_factory=dict)
    attributes: set[str] = field(default_factory=set)
    lines: list[str] = field(default_factory=list)


@dataclass
class OptionalDereference:
    variable: str
    assigned_line: int
    assigned_source: str
    deref_line: int
    deref_source: str
    call_name: str
    callee: FunctionInfo | None = None
    is_get: bool = False


@dataclass
class Nullability:
    verdict: str            # "nullable" | "non-null" | "unknown"
    reason: str
    line: int = 0
    source: str = ""


@dataclass
class UnboundedBound:
    container: str
    counter: str
    index_line: int
    index_source: str
    counter_line: int
    counter_source: str
    clamp_checked: bool


class SymbolIndex:
    def __init__(self, root: Path, files: list[str]):
        self.root = root
        self.files = files
        self.functions: dict[str, list[FunctionInfo]] = {}
        self.classes: dict[str, list[ClassInfo]] = {}
        self.module_constants: dict[str, dict[str, tuple[int, str]]] = {}
        self.identifier_files: dict[str, set[str]] = {}
        self._trees: dict[str, ast.AST] = {}
        self._sources: dict[str, list[str]] = {}
        self._build()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        for rel in self.files:
            if not rel.endswith(".py"):
                continue
            path = self.root / rel
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(text)
            except (OSError, SyntaxError, ValueError):
                continue
            self._sources[rel] = text.splitlines()
            self._trees[rel] = tree
            self._index_file(rel, tree)

    def _index_file(self, rel: str, tree: ast.Module) -> None:
        lines = self._sources.get(rel, [])
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        self.module_constants.setdefault(rel, {})[target.id] = (
                            node.lineno,
                            ast.unparse(node.value),
                        )
            elif isinstance(node, ast.ClassDef):
                cls = ClassInfo(rel, rel, node.lineno, lines=lines)
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        info = FunctionInfo(
                            name=sub.name,
                            qualname=f"{node.name}.{sub.name}",
                            file=rel,
                            lineno=sub.lineno,
                            end_lineno=getattr(sub, "end_lineno", sub.lineno),
                            cls=node.name,
                            node=sub,
                            source=lines,
                        )
                        cls.methods[sub.name] = info
                        self.functions.setdefault(sub.name, []).append(info)
                    elif isinstance(sub, ast.Assign):
                        for target in sub.targets:
                            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                                cls.attributes.add(target.attr)
                self.classes.setdefault(node.name, []).append(cls)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info = FunctionInfo(
                    name=node.name,
                    qualname=node.name,
                    file=rel,
                    lineno=node.lineno,
                    end_lineno=getattr(node, "end_lineno", node.lineno),
                    cls=None,
                    node=node,
                    source=lines,
                )
                self.functions.setdefault(node.name, []).append(info)

        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.identifier_files.setdefault(node.id, set()).add(rel)
            elif isinstance(node, ast.Attribute):
                self.identifier_files.setdefault(node.attr, set()).add(rel)

    # --------------------------------------------------------------- accessors
    def source_lines(self, rel: str) -> list[str]:
        return self._sources.get(rel, [])

    def function_at(self, rel: str, line: int) -> FunctionInfo | None:
        best: FunctionInfo | None = None
        for infos in self.functions.values():
            for info in infos:
                if info.file == rel and info.lineno <= line <= info.end_lineno:
                    if best is None or (info.end_lineno - info.lineno) < (best.end_lineno - best.lineno):
                        best = info
        return best

    def resolve(self, name: str) -> FunctionInfo | None:
        candidates = self.functions.get(name)
        if not candidates:
            return None
        return sorted(candidates, key=lambda i: i.file)[0]

    def resolve_method(self, cls: str, method: str) -> FunctionInfo | None:
        for info in self.classes.get(cls, []):
            if method in info.methods:
                return info.methods[method]
        return None

    def class_of(self, qualname: str) -> ClassInfo | None:
        cls = qualname.split(".")[0]
        infos = self.classes.get(cls)
        return infos[0] if infos else None

    def referencing_files(self, identifier: str) -> set[str]:
        return set(self.identifier_files.get(identifier, set()))

    def constant_is_dangling(self, rel: str, name: str) -> tuple[bool, list[str]]:
        """A constant declared in ``rel`` but never referenced anywhere."""
        if name not in self.module_constants.get(rel, {}):
            return False, []
        referencing = {
            f for f in self.identifier_files.get(name, set()) if f != rel
        }
        own_refs = self._own_identifiers(rel)
        return not referencing and name not in own_refs, []

    def _own_identifiers(self, rel: str) -> set[str]:
        tree = self._trees.get(rel)
        names: set[str] = set()
        if tree is None:
            return names
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                names.add(node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                names.add(node.attr)
        return names

    # --------------------------------------------------------------- detectors
    def optional_dereference(self, info: FunctionInfo, error_line: int) -> OptionalDereference | None:
        """Find a local bound to a lookup/call and then dereferenced.

        Records the callee too, including callees that cannot return ``None`` --
        the analyzer needs to compare the *evidence* against the code at the
        analysed sha, and "this can no longer be None" is a finding in itself.
        """
        node = info.node
        if node is None:
            return None
        candidates: dict[str, tuple[int, str, str, FunctionInfo | None, bool, bool]] = {}
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                is_get = (
                    isinstance(call.func, ast.Attribute) and call.func.attr == "get"
                ) or (isinstance(call.func, ast.Name) and call.func.id == "get")
                callee = self.resolve_call_target(call)
                if not is_get and callee is None:
                    continue
                nullable = callee is not None and self.nullability(callee).verdict == "nullable"
                call_name = ast.unparse(call)
                for target in stmt.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    existing = candidates.get(target.id)
                    if existing and (existing[4] or not nullable):
                        continue
                    candidates[target.id] = (stmt.lineno, ast.unparse(stmt), call_name, callee, nullable, is_get)
        if not candidates:
            return None

        hits: list[OptionalDereference] = []
        for stmt in ast.walk(node):
            hit_line = getattr(stmt, "lineno", 0)
            if hit_line < error_line - 40:
                continue
            for name, (line, assign_src, call_name, callee, nullable, is_get) in candidates.items():
                if hit_line <= line:
                    continue
                if self._dereferences(stmt, name):
                    hits.append(
                        OptionalDereference(
                            variable=name,
                            assigned_line=line,
                            assigned_source=assign_src,
                            deref_line=hit_line,
                            deref_source=info.line_text(hit_line),
                            call_name=call_name,
                            callee=callee,
                            is_get=is_get,
                        )
                    )
        if not hits:
            return None
        nullable_hits = [hit for hit in hits if hit.callee is not None and self.nullability(hit.callee).verdict == "nullable"]
        if nullable_hits:
            return nullable_hits[0]
        return hits[0]

    def resolve_call_target(self, call: ast.Call) -> FunctionInfo | None:
        """Resolve ``self.m(...)`` / ``m(...)`` to a repository function."""
        if isinstance(call.func, ast.Attribute):
            name = call.func.attr
        elif isinstance(call.func, ast.Name):
            name = call.func.id
        else:
            return None
        candidates = self.functions.get(name, [])
        if not candidates:
            return None
        return sorted(candidates, key=lambda info: info.file)[0]

    def _nullable_callee(self, call: ast.Call) -> FunctionInfo | None:
        """Resolve ``self.m(...)`` / ``m(...)`` to a method that may return None."""
        target = self.resolve_call_target(call)
        if target is None:
            return None
        return target if self.nullability(target).verdict == "nullable" else None

    @staticmethod
    def _dereferences(stmt: ast.AST, name: str) -> bool:
        if isinstance(stmt, ast.Subscript) and isinstance(stmt.value, ast.Name) and stmt.value.id == name:
            return True
        if isinstance(stmt, ast.For) and isinstance(stmt.iter, ast.Name) and stmt.iter.id == name:
            return True
        if isinstance(stmt, ast.Attribute) and isinstance(stmt.value, ast.Name) and stmt.value.id == name:
            return True
        if isinstance(stmt, ast.Call):
            for arg in list(stmt.args) + [kw.value for kw in stmt.keywords]:
                if isinstance(arg, ast.Name) and arg.id == name:
                    return True
            if isinstance(stmt.func, ast.Name) and stmt.func.id == name:
                return True
        if isinstance(stmt, ast.Subscript) and isinstance(stmt.value, ast.Call):
            if any(isinstance(a, ast.Name) and a.id == name for a in stmt.value.args):
                return True
        return False

    def nullability(self, info: FunctionInfo) -> Nullability:
        """Can ``info`` return ``None`` for some input?"""
        node = info.node
        if node is None:
            return Nullability("unknown", "source not parseable")
        returns: list[ast.Return] = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
        if not returns:
            return Nullability("unknown", "no return statement")
        for ret in returns:
            value = ret.value
            if value is None or isinstance(value, ast.Constant) and value.value is None:
                return Nullability(
                    "nullable",
                    "function has an explicit `return None` / bare return",
                    ret.lineno,
                    info.line_text(ret.lineno),
                )
            if isinstance(value, ast.Call):
                func = value.func
                if isinstance(func, ast.Attribute) and func.attr == "get" and len(value.args) < 2:
                    return Nullability(
                        "nullable",
                        f"`{ast.unparse(func)}` has no default argument, so it returns None on a miss",
                        ret.lineno,
                        info.line_text(ret.lineno),
                    )
                if isinstance(func, ast.Name) and func.id in {"get", "find", "pop"} and len(value.args) < 2:
                    return Nullability(
                        "nullable",
                        f"`{ast.unparse(func)}` may return None",
                        ret.lineno,
                        info.line_text(ret.lineno),
                    )
            if isinstance(value, ast.BoolOp):
                for value_item in value.values:
                    if isinstance(value_item, ast.Constant) and value_item.value is None:
                        return Nullability(
                            "nullable", "returns a conditional `None`", ret.lineno, info.line_text(ret.lineno)
                        )
            if isinstance(value, ast.IfExp):
                branches = [value.body, value.orelse]
                if any(isinstance(b, ast.Constant) and b.value is None for b in branches):
                    return Nullability(
                        "nullable", "conditional expression yields None", ret.lineno, info.line_text(ret.lineno)
                    )
        return Nullability(
            "non-null",
            "every return path resolves a concrete value",
            returns[-1].lineno,
            info.line_text(returns[-1].lineno),
        )

    def unbounded_bound(self, info: FunctionInfo) -> UnboundedBound | None:
        """Slice/index of a fixed container bounded by an unclamped counter."""
        cls = self.class_of(info.qualname) if "." in info.qualname else None
        node = info.node
        if node is None:
            return None
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.Subscript):
                continue
            container = None
            bound = None
            if isinstance(stmt.value, ast.Attribute) and isinstance(stmt.value.value, ast.Name):
                container = stmt.value.attr
            if stmt.slice is None:
                continue
            bounds: list[ast.AST] = []
            if isinstance(stmt.slice, ast.Slice):
                if stmt.slice.upper is not None:
                    bounds.append(stmt.slice.upper)
            elif isinstance(stmt.slice, ast.BinOp):
                bounds.append(stmt.slice.right)
            for candidate in bounds:
                if (
                    isinstance(candidate, ast.Attribute)
                    and isinstance(candidate.value, ast.Name)
                    and candidate.value.id == "self"
                ):
                    bound = candidate.attr
            if not container or not bound:
                continue
            counter_line, counter_source = self._counter_growth(cls, bound)
            if counter_line == 0:
                continue
            clamped = self._is_clamped(cls, bound, container)
            if clamped:
                continue
            return UnboundedBound(
                container=container,
                counter=bound,
                index_line=stmt.lineno,
                index_source=info.line_text(stmt.lineno),
                counter_line=counter_line,
                counter_source=counter_source,
                clamp_checked=False,
            )
        return None

    @staticmethod
    def _counter_growth(cls: ClassInfo | None, counter: str) -> tuple[int, str]:
        if cls is None:
            return 0, ""
        for method in cls.methods.values():
            for stmt in ast.walk(method.node):
                if (
                    isinstance(stmt, ast.AugAssign)
                    and isinstance(stmt.target, ast.Attribute)
                    and stmt.target.attr == counter
                ):
                    return stmt.lineno, method.line_text(stmt.lineno)
        return 0, ""

    @staticmethod
    def _is_clamped(cls: ClassInfo | None, counter: str, container: str) -> bool:
        if cls is None:
            return False
        for method in cls.methods.values():
            for stmt in ast.walk(method.node):
                if isinstance(stmt, ast.Compare):
                    names = {n.attr for n in ast.walk(stmt) if isinstance(n, ast.Attribute)}
                    calls = {
                        ast.unparse(n.func)
                        for n in ast.walk(stmt)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    }
                    if counter in names and (container in names or any("len" in c for c in calls)):
                        return True
                if isinstance(stmt, ast.Call) and isinstance(stmt.func, ast.Name) and stmt.func.id == "min":
                    names = {n.attr for n in ast.walk(stmt) if isinstance(n, ast.Attribute)}
                    if counter in names:
                        return True
                if isinstance(stmt, ast.Call) and isinstance(stmt.func, ast.Attribute):
                    if stmt.func.attr in {"append"}:
                        continue
                    if stmt.func.attr == "insert":
                        return True
        return False

    def constant_candidates(self, rel: str) -> dict[str, tuple[int, str]]:
        return dict(self.module_constants.get(rel, {}))

    def class_attributes(self, qualname: str) -> set[str]:
        cls = self.class_of(qualname)
        return set(cls.attributes) if cls else set()

    # ------------------------------------------------------------- provenance
    def calls_in_function(self, info: FunctionInfo) -> list[tuple[str, int]]:
        """``(callee name, line)`` for every call made inside ``info``."""
        if info.node is None:
            return []
        calls: list[tuple[str, int]] = []
        for stmt in ast.walk(info.node):
            if isinstance(stmt, ast.Call):
                name = ""
                if isinstance(stmt.func, ast.Attribute):
                    name = stmt.func.attr
                elif isinstance(stmt.func, ast.Name):
                    name = stmt.func.id
                if name:
                    calls.append((name, stmt.lineno))
        return calls

    def arithmetic_provenance(self, info: FunctionInfo) -> list[tuple[str, int, str]]:
        """Locals whose value comes from arithmetic, with their origin line."""
        if info.node is None:
            return []
        found: list[tuple[str, int, str]] = []
        for stmt in ast.walk(info.node):
            if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.BinOp):
                continue
            if not isinstance(stmt.value.op, (ast.Mult, ast.Div, ast.Add, ast.Sub)):
                continue
            if not self._touches_number_constructor(stmt.value):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    found.append((target.id, stmt.lineno, info.line_text(stmt.lineno)))
        return found

    def returns_arithmetic(self, info: FunctionInfo) -> list[tuple[int, str, str]]:
        """Return statements whose value was produced by arithmetic."""
        arithmetic = {name: (line, src) for name, line, src in self.arithmetic_provenance(info)}
        results: list[tuple[int, str, str]] = []
        if info.node is None:
            return results
        for stmt in ast.walk(info.node):
            if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Name):
                origin = arithmetic.get(stmt.value.id)
                if origin:
                    results.append((stmt.lineno, info.line_text(stmt.lineno), origin[1]))
            elif isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.BinOp):
                if isinstance(stmt.value.op, (ast.Mult, ast.Div, ast.Add, ast.Sub)):
                    results.append((stmt.lineno, info.line_text(stmt.lineno), info.line_text(stmt.lineno)))
        return results

    def normalisation_calls(self, info: FunctionInfo) -> list[tuple[int, str]]:
        if info.node is None:
            return []
        hits: list[tuple[int, str]] = []
        for stmt in ast.walk(info.node):
            if isinstance(stmt, ast.Call) and isinstance(stmt.func, ast.Attribute):
                if stmt.func.attr in {"quantize", "normalize", "round", "quantise"}:
                    hits.append((stmt.lineno, info.line_text(stmt.lineno)))
        return hits

    @staticmethod
    def _touches_number_constructor(node: ast.AST) -> bool:
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                if child.func.id in {"Decimal", "int", "float", "Fraction"}:
                    return True
            if isinstance(child, ast.Attribute) and child.attr in {"quantize", "normalize", "round"}:
                return True
        return False
