"""Command line: ``evalkit lint`` and ``evalkit init``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Sequence
from dataclasses import asdict
from pathlib import Path

from evalkit import lint, rules, scaffold
from evalkit.spec import (SKILL_FILE, SUITE_FILE, Case, SkillSpec, SpecError, load_cases,
                          load_skill_dir, load_subject)

SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv"})


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    code: int = args.handler(args)
    return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evalkit", description=__doc__)
    commands = parser.add_subparsers(required=True, metavar="command")

    lint_cmd = commands.add_parser("lint", help="check subjects against Anthropic's guidance")
    lint_cmd.add_argument("paths", nargs="+", type=Path,
                          help="skill directories (searched recursively), *.skill.toml or "
                               "*.prompt.toml files")
    lint_cmd.add_argument("--format", choices=("text", "json"), default="text")
    lint_cmd.add_argument("--min-severity", choices=("info", "warn", "error"), default="info",
                          help="hide findings below this level")
    lint_cmd.set_defaults(handler=_lint)

    init_cmd = commands.add_parser("init", help=f"write a starter {SUITE_FILE} for a skill")
    init_cmd.add_argument("skill", type=Path, help="skill directory")
    init_cmd.add_argument("--force", action="store_true", help="overwrite an existing suite")
    init_cmd.set_defaults(handler=_init)
    return parser


def _lint(args: argparse.Namespace) -> int:
    findings: list[lint.Finding] = []
    for path in _expand(args.paths):
        findings += _lint_one(path)
    floor = lint.Severity[args.min_severity.upper()]
    shown = [f for f in findings if f.severity >= floor]
    if args.format == "json":
        print(json.dumps([{**asdict(f), "severity": str(f.severity)} for f in shown], indent=2))
    else:
        _print_text(shown)
    return 1 if any(f.severity is lint.Severity.ERROR for f in findings) else 0


def _lint_one(path: Path) -> list[lint.Finding]:
    try:
        subject = load_subject(path)
        cases = _cases_for(subject)
    except SpecError as exc:
        return [lint.Finding("spec", lint.Severity.ERROR, str(exc.path), str(exc).split(": ", 1)[-1],
                             "https://github.com/baileyrd/eval-kit")]
    return lint.run(lint.LintContext(subject, cases), rules.RULES)


def _cases_for(subject: object) -> tuple[Case, ...] | None:
    if not isinstance(subject, SkillSpec):
        return None
    suite = scaffold.suite_path(subject)
    return load_cases(suite) if suite.is_file() else None


def _expand(paths: Sequence[Path]) -> Iterator[Path]:
    """Files as given; a directory is a skill, or is searched for skills."""
    for path in paths:
        if not path.is_dir() or (path / SKILL_FILE).is_file():
            yield path
            continue
        found = sorted(p.parent for p in path.rglob(SKILL_FILE)
                       if not SKIP_DIRS & set(p.relative_to(path).parts))
        if not found:
            print(f"evalkit: no {SKILL_FILE} under {path}", file=sys.stderr)
        yield from found


def _print_text(findings: Sequence[lint.Finding]) -> None:
    for f in findings:
        print(f"{f.location}: {f.severity}: {f.rule}: {f.message}")
    counts = {s: sum(1 for f in findings if f.severity is s) for s in lint.Severity}
    print(f"{counts[lint.Severity.ERROR]} error(s), {counts[lint.Severity.WARN]} warning(s), "
          f"{counts[lint.Severity.INFO]} note(s)")


def _init(args: argparse.Namespace) -> int:
    try:
        skill = load_skill_dir(args.skill, frozenset({"claude-code"}))
        path = scaffold.write_suite(skill, args.force)
    except (SpecError, FileExistsError) as exc:
        print(f"evalkit: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {path}; replace its TODO inputs before running it")
    return 0
