"""Checks: pure judgements of a trace.

A check is written in a suite as an inline table, for example
``{ regex = "data-bind" }`` or ``{ contains = "OK", in = "out.txt" }``.
:func:`parse` turns that table into a typed check, or raises ``ValueError``.

A ``judge`` check is pure too: it reads a verdict a model already gave and the
store cached (see :mod:`evalkit.judge`), passed in as ``verdicts``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from evalkit.trace import Trace


@dataclass(frozen=True)
class Contains:
    needle: str
    target: str | None  # None = the final text; otherwise a file path

    def passes(self, trace: Trace, verdicts: Mapping[str, bool]) -> bool:
        text = _target_text(trace, self.target)
        return text is not None and self.needle in text

    def describe(self) -> str:
        return f"contains {self.needle!r}{_where(self.target)}"


@dataclass(frozen=True)
class Regex:
    pattern: str
    target: str | None

    def passes(self, trace: Trace, verdicts: Mapping[str, bool]) -> bool:
        text = _target_text(trace, self.target)
        return text is not None and re.search(self.pattern, text, re.MULTILINE) is not None

    def describe(self) -> str:
        return f"matches /{self.pattern}/{_where(self.target)}"


@dataclass(frozen=True)
class FileExists:
    path: str

    def passes(self, trace: Trace, verdicts: Mapping[str, bool]) -> bool:
        return trace.has_file(self.path)

    def describe(self) -> str:
        return f"file {self.path} exists"


@dataclass(frozen=True)
class ToolCalled:
    name: str

    def passes(self, trace: Trace, verdicts: Mapping[str, bool]) -> bool:
        return any(call.name == self.name for call in trace.tool_calls)

    def describe(self) -> str:
        return f"called {self.name}"


@dataclass(frozen=True)
class AllOf:
    checks: tuple[Check, ...]

    def passes(self, trace: Trace, verdicts: Mapping[str, bool]) -> bool:
        return all(check.passes(trace, verdicts) for check in self.checks)

    def describe(self) -> str:
        return "all(" + "; ".join(c.describe() for c in self.checks) + ")"


@dataclass(frozen=True)
class AnyOf:
    checks: tuple[Check, ...]

    def passes(self, trace: Trace, verdicts: Mapping[str, bool]) -> bool:
        return any(check.passes(trace, verdicts) for check in self.checks)

    def describe(self) -> str:
        return "any(" + "; ".join(c.describe() for c in self.checks) + ")"


@dataclass(frozen=True)
class Judge:
    rubric: str

    def passes(self, trace: Trace, verdicts: Mapping[str, bool]) -> bool:
        return verdicts[self.rubric]

    def describe(self) -> str:
        return f"judge: {self.rubric}"


Check = Contains | Regex | FileExists | ToolCalled | AllOf | AnyOf | Judge


def rubrics(check: Check) -> tuple[str, ...]:
    """Every judge rubric in *check*, including inside all/any."""
    if isinstance(check, Judge):
        return (check.rubric,)
    if isinstance(check, (AllOf, AnyOf)):
        return tuple(r for child in check.checks for r in rubrics(child))
    return ()


def parse(raw: dict[str, object]) -> Check:
    """Build a check from its inline-table form."""
    kinds = set(raw) - {"in"}
    if len(kinds) != 1:
        raise ValueError(f"a check needs exactly one kind, got {sorted(kinds) or 'none'}")
    (kind,) = kinds
    value = raw[kind]
    target = raw.get("in")
    if target is not None and (kind not in ("contains", "regex") or not isinstance(target, str)):
        raise ValueError(f"'in' is a file path and applies only to contains and regex")
    if kind in ("all", "any"):
        if not isinstance(value, list) or not value or not all(isinstance(v, dict) for v in value):
            raise ValueError(f"'{kind}' takes a non-empty array of checks")
        children = tuple(parse(v) for v in value)
        return AllOf(children) if kind == "all" else AnyOf(children)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{kind}' takes a non-empty string")
    if kind == "contains":
        return Contains(value, target)
    if kind == "regex":
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regex {value!r}: {exc}") from None
        return Regex(value, target)
    if kind == "file_exists":
        return FileExists(value)
    if kind == "tool_called":
        return ToolCalled(value)
    if kind == "judge":
        return Judge(value)
    raise ValueError(f"unknown check '{kind}'; use contains, regex, file_exists, tool_called, "
                     "judge, all or any")


def _target_text(trace: Trace, target: str | None) -> str | None:
    return trace.final_text if target is None else trace.file_text(target)


def _where(target: str | None) -> str:
    return "" if target is None else f" in {target}"
