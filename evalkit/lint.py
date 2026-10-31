"""Static evaluation: check a subject against Anthropic's published guidance.

No model calls. Each rule in :mod:`evalkit.rules` is a pure function from a
:class:`LintContext` to findings.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import IntEnum

from evalkit.spec import Case, Subject


class Severity(IntEnum):
    INFO = 0
    WARN = 1
    ERROR = 2

    def __str__(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: Severity
    location: str
    message: str
    source: str  # URL of the guidance the rule enforces


@dataclass(frozen=True)
class LintContext:
    subject: Subject
    cases: tuple[Case, ...] | None  # None when no suite was found


Rule = Callable[[LintContext], Iterable[Finding]]


def run(context: LintContext, rules: Iterable[Rule]) -> list[Finding]:
    """Apply every rule; most severe first, then by location."""
    findings = [finding for rule in rules for finding in rule(context)]
    return sorted(findings, key=lambda f: (-f.severity, f.location, f.rule))
