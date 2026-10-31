"""Command line: lint, init, run, score, compare, calibrate."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict
from pathlib import Path

from evalkit import engine, judge, lint, plan, render, rules, scaffold, stats
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

    run_cmd = commands.add_parser("run", help="run what has no stored trace, then judge "
                                             "what has no cached verdict")
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

    calibrate_cmd = commands.add_parser(
        "calibrate", help="check the judge against your labels (writes a template first)")
    _suite_args(calibrate_cmd)
    calibrate_cmd.add_argument("labels", type=Path, help="labels JSONL file")
    calibrate_cmd.set_defaults(handler=_calibrate)
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
    runner = CliRunner(suite)
    failures = engine.execute(todo, runner, store, version, args.jobs, _progress(len(todo)))
    to_judge = judge.pending(jobs, store, suite.judge)
    if to_judge:
        print(f"{len(to_judge)} judge call(s) with {suite.judge.model}")
        failures += engine.execute_judging(to_judge, runner, suite.judge, store, args.jobs,
                                           _progress(len(to_judge)))
    print(f"stored in {store.path}; {failures} failed (re-run to retry them)")
    return 1 if failures else 0


def _progress(total: int) -> Callable[[str, Exception | None], None]:
    done = 0

    def report(label: str, error: Exception | None) -> None:
        nonlocal done
        done += 1
        print(f"[{done}/{total}] {label}: {'ok' if error is None else f'FAILED: {error}'}")

    return report


def _score(args: argparse.Namespace) -> int:
    try:
        suite, store, jobs = _load(args)
    except (SpecError, ValueError) as exc:
        return _fail(exc)
    scored = engine.score(jobs, store, suite.judge)
    outcomes = scored.outcomes
    expected = len(suite.cases) * suite.samples
    print(f"{'variant':<20} {'runs':>9} {'pass':>6} {'triggered':>10}")
    for row in stats.summarise(outcomes):
        trigger = "-" if row.trigger_rate is None else f"{row.trigger_rate:.0%}"
        print(f"{row.variant:<20} {row.runs:>4}/{expected:<4} {row.pass_rate:>6.0%} {trigger:>10}")
    if scored.unjudged:
        print(f"{scored.unjudged} stored run(s) await a judge verdict; 'evalkit run' judges them")
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
        result = stats.compare(engine.score(jobs, store, suite.judge).outcomes, base.id, head.id)
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


def _calibrate(args: argparse.Namespace) -> int:
    try:
        suite, store, jobs = _load(args)
        if not args.labels.exists():
            count = _write_label_template(args.labels, jobs, store, suite)
            print(f"wrote {count} judged verdict(s) to {args.labels}; set each \"label\" to "
                  "true or false (or delete the line), then run calibrate again")
            return 0
        pairs, disagreements = _labelled_pairs(args.labels, jobs, store, suite)
    except (SpecError, ValueError, OSError) as exc:
        return _fail(exc)
    result = judge.agreement(pairs)
    print(f"{result.agreed}/{result.labelled} agree ({result.rate:.0%}); judge too lenient "
          f"{result.judge_too_lenient}, too strict {result.judge_too_strict}")
    for line in disagreements:
        print(f"  {line}")
    return 0


def _label_rows(jobs: Sequence[plan.Job], store: JsonlStore,
                suite: Suite) -> Iterator[tuple[dict[str, object], bool, str, str]]:
    """Each cached verdict as (row identity, judge verdict, judge reason, reply excerpt)."""
    for job in jobs:
        record = store.get(job.key)
        if record is None:
            continue
        for rubric, (passed, reason) in (judge.verdicts_for(job, record.trace, store, suite.judge)
                                         or {}).items():
            row: dict[str, object] = {"variant": job.variant.id, "case": job.case.id,
                                      "sample": job.sample, "rubric": rubric}
            yield row, passed, reason, record.trace.final_text[:300]


def _write_label_template(path: Path, jobs: Sequence[plan.Job], store: JsonlStore,
                          suite: Suite) -> int:
    lines = []
    for row, passed, reason, excerpt in _label_rows(jobs, store, suite):
        lines.append(json.dumps({**row, "label": None, "judge": passed, "reason": reason,
                                 "excerpt": excerpt}, ensure_ascii=False))
    if not lines:
        raise ValueError("no judged runs yet; add judge checks and run the suite first")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def _labelled_pairs(path: Path, jobs: Sequence[plan.Job], store: JsonlStore,
                    suite: Suite) -> tuple[list[tuple[bool, bool]], list[str]]:
    judged = {_row_id(row): (passed, reason)
              for row, passed, reason, _ in _label_rows(jobs, store, suite)}
    pairs: list[tuple[bool, bool]] = []
    disagreements: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        label = row.get("label")
        if label is None:
            continue
        if not isinstance(label, bool):
            raise ValueError(f"{path}:{number}: label must be true, false or null")
        found = judged.get(_row_id(row))
        if found is None:
            raise ValueError(f"{path}:{number}: no current judge verdict for this row "
                             "(the trace or rubric changed since the template was written)")
        pairs.append((found[0], label))
        if found[0] != label:
            disagreements.append(f"{row['variant']} {row['case']} #{row['sample']} "
                                 f"{row['rubric'][:50]!r}: judge {found[0]}, you {label} "
                                 f"({found[1]})")
    return pairs, disagreements


def _row_id(row: dict[str, object]) -> tuple[str, str, str, str]:
    return (str(row["variant"]), str(row["case"]), str(row["sample"]), str(row["rubric"]))


def _fail(error: object) -> int:
    print(f"evalkit: {error}", file=sys.stderr)
    return 1
