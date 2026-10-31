"""Command line: lint, init, run, score, compare."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Sequence
from dataclasses import asdict
from pathlib import Path

from evalkit import engine, lint, plan, render, rules, scaffold, stats
from evalkit.runner import CliRunner, RunError, cli_version
from evalkit.spec import (SKILL_FILE, SUITE_FILE, Case, SkillSpec, SpecError, Suite, Variant,
                          load_cases,
                          load_skill_dir, load_subject, load_suite)
from evalkit.store import JsonlStore

SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv"})
CONFIRM_ABOVE = 50  # jobs; more than this needs --yes


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

    run_cmd = commands.add_parser("run", help="run the jobs a suite has no stored trace for")
    _suite_args(run_cmd)
    run_cmd.add_argument("--jobs", type=int, default=1, help="runs in parallel (default 1)")
    run_cmd.add_argument("--yes", action="store_true",
                         help=f"don't stop when more than {CONFIRM_ABOVE} runs are needed")
    run_cmd.set_defaults(handler=_run)

    score_cmd = commands.add_parser("score", help="judge stored traces; no model calls")
    _suite_args(score_cmd)
    score_cmd.add_argument("--verbose", action="store_true", help="list each failed run")
    score_cmd.set_defaults(handler=_score)

    compare_cmd = commands.add_parser("compare", help="paired lift of HEAD over BASE")
    _suite_args(compare_cmd)
    compare_cmd.add_argument("base")
    compare_cmd.add_argument("head")
    compare_cmd.set_defaults(handler=_compare)
    return parser


def _suite_args(command: argparse.ArgumentParser) -> None:
    command.add_argument("suite", type=Path, help="suite TOML file")
    command.add_argument("--store", type=Path,
                         help="trace file (default: .evalkit/<suite>.jsonl beside the suite)")


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


def _load(args: argparse.Namespace) -> tuple[Suite, JsonlStore, list[plan.Job]]:
    suite = load_suite(args.suite)
    store_path = args.store or suite.path.parent / ".evalkit" / f"{suite.path.stem}.jsonl"
    return suite, JsonlStore(store_path), plan.jobs(suite)


def _run(args: argparse.Namespace) -> int:
    try:
        suite, store, jobs = _load(args)
        version = cli_version()
    except (SpecError, RunError, ValueError) as exc:
        return _fail(exc)
    todo = engine.pending(jobs, store, version)
    print(f"{len(jobs)} runs in the suite, {len(todo)} to do ({version})")
    if len(todo) > CONFIRM_ABOVE and not args.yes:
        return _fail(f"{len(todo)} runs is more than {CONFIRM_ABOVE}; pass --yes to go ahead")
    done = 0

    def report(job: plan.Job, error: Exception | None) -> None:
        nonlocal done
        done += 1
        status = "ok" if error is None else f"FAILED: {error}"
        print(f"[{done}/{len(todo)}] {job.variant.id} {job.case.id} #{job.sample}: {status}")

    failures = engine.execute(todo, CliRunner(suite), store, version, max(1, args.jobs), report)
    print(f"stored in {store.path}; {failures} failed (re-run to retry them)")
    return 1 if failures else 0


def _score(args: argparse.Namespace) -> int:
    try:
        suite, store, jobs = _load(args)
    except (SpecError, ValueError) as exc:
        return _fail(exc)
    outcomes = engine.score(jobs, store)
    expected = len(suite.cases) * suite.samples
    print(f"{'variant':<20} {'runs':>9} {'pass':>6} {'triggered':>10}")
    for row in stats.summarise(outcomes):
        trigger = "-" if row.trigger_rate is None else f"{row.trigger_rate:.0%}"
        print(f"{row.variant:<20} {row.runs:>4}/{expected:<4} {row.pass_rate:>6.0%} {trigger:>10}")
    if args.verbose:
        for o in outcomes:
            if not o.passed:
                print(f"  {o.variant} {o.case} #{o.sample}: failed {'; '.join(o.failed_checks)}")
    return 0


def _compare(args: argparse.Namespace) -> int:
    try:
        suite, store, jobs = _load(args)
        base, head = suite.variant(args.base), suite.variant(args.head)
        _require_same_inputs(suite, base, head)
        result = stats.compare(engine.score(jobs, store), base.id, head.id)
    except (SpecError, ValueError) as exc:
        return _fail(exc)
    print(f"{head.id} vs {base.id} over {result.cases} case(s)")
    print(f"lift {result.lift:+.0%}  (95% interval {result.low:+.0%} to {result.high:+.0%})")
    if result.lift_when_triggered is not None:
        skill = head.skill.name if head.skill else head.id
        print(f"lift on runs where {skill} loaded {result.lift_when_triggered:+.0%}")
    print(f"improved: {', '.join(result.improved) or 'none'}")
    print(f"regressed: {', '.join(result.regressed) or 'none'}")
    return 0


def _require_same_inputs(suite: Suite, base: Variant, head: Variant) -> None:
    """A paired comparison needs both variants to receive the same task text."""
    for case in suite.cases:
        if render.user_turn(base, case) != render.user_turn(head, case):
            raise ValueError(f"{base.id!r} and {head.id!r} send different input for case "
                             f"{case.id!r}; compare a prompt against a baseline prompt that "
                             "uses the same [user] template and documents")


def _fail(error: object) -> int:
    print(f"evalkit: {error}", file=sys.stderr)
    return 1
