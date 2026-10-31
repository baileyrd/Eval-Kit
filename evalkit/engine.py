"""Run the jobs a suite still needs, judge what needs judging, and score what is stored."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Protocol, TypeVar

from evalkit import checks, judge
from evalkit.plan import Job
from evalkit.spec import Case, JudgeConfig, Variant
from evalkit.store import JsonlStore, Record, Verdict
from evalkit.trace import Trace

Item = TypeVar("Item")
Result = TypeVar("Result")


class Runner(Protocol):
    def run(self, variant: Variant, case: Case) -> Trace: ...


class Asker(Protocol):
    def ask(self, judge: JudgeConfig, system: str, user_text: str) -> str: ...


@dataclass(frozen=True)
class Outcome:
    """One stored run, judged by its case's checks."""

    variant: str
    case: str
    sample: int
    passed: bool
    failed_checks: tuple[str, ...]
    triggered: bool | None  # None when the variant has no skill
    checks: tuple[tuple[str, bool], ...] = ()  # each check's description and result


@dataclass(frozen=True)
class Scored:
    outcomes: list[Outcome]
    unjudged: int  # stored runs left out because a judge verdict is missing


def pending(jobs: Sequence[Job], store: JsonlStore, cli_version: str) -> list[Job]:
    """Jobs with no stored trace from this CLI version."""
    out = []
    for job in jobs:
        record = store.get(job.key)
        if record is None or record.cli_version != cli_version:
            out.append(job)
    return out


def execute(todo: Sequence[Job], runner: Runner, store: JsonlStore, cli_version: str,
            workers: int, report: Callable[[str, Exception | None], None],
            judging: tuple[Asker, JudgeConfig] | None = None) -> int:
    """Run *todo* and store each success. Failures are reported, not stored, so the
    next ``run`` retries them. Only this thread writes the store. Returns failures.

    With *judging*, each worker also judges its own trace as soon as the run ends,
    so judge calls overlap the remaining runs instead of waiting for all of them.
    """

    def work(job: Job) -> tuple[Trace, list[tuple[judge.JudgeJob, Verdict | Exception]]]:
        trace = runner.run(job.variant, job.case)
        if judging is None:
            return trace, []
        asker, config = judging
        return trace, [(item, _try_ask(asker, config, item))
                       for item in judge.jobs_for(job, trace, config)]

    failures = 0
    for job, result in _parallel(todo, work, workers):
        label = f"{job.variant.id} {job.case.id} #{job.sample}"
        if isinstance(result, Exception):
            failures += 1
            report(label, result)
            continue
        trace, judged = result
        store.put(Record(job.key, job.variant.id, job.case.id, job.sample, cli_version, trace))
        errors = [v for _, v in judged if isinstance(v, Exception)]
        for _, verdict in judged:
            if isinstance(verdict, Verdict):
                store.put_verdict(verdict)
        failures += len(errors)
        report(label, JudgeFailed(len(errors), errors[0]) if errors else None)
    return failures


class JudgeFailed(RuntimeError):
    """The run succeeded and was stored, but some of its judge calls failed."""

    def __init__(self, count: int, first: Exception) -> None:
        super().__init__(f"run stored, {count} judge call(s) failed: {first}")


def execute_judging(todo: Sequence[judge.JudgeJob], asker: Asker, config: JudgeConfig,
                    store: JsonlStore, workers: int,
                    report: Callable[[str, Exception | None], None]) -> int:
    """Ask the judge for each missing verdict and cache it. Returns failures."""
    failures = 0
    for item, result in _parallel(todo, lambda i: _ask(asker, config, i), workers):
        label = f"judge {item.label}: {item.rubric[:50]}"
        if isinstance(result, Exception):
            failures += 1
            report(label, result)
            continue
        store.put_verdict(result)
        report(label, None)
    return failures


def _ask(asker: Asker, config: JudgeConfig, item: judge.JudgeJob) -> Verdict:
    reply = asker.ask(config, judge.SYSTEM, judge.prompt(item.rubric, item.trace))
    passed, reason = judge.parse_verdict(reply)
    return Verdict(item.key, passed, reason)


def _try_ask(asker: Asker, config: JudgeConfig, item: judge.JudgeJob) -> Verdict | Exception:
    try:
        return _ask(asker, config, item)
    except Exception as exc:  # one bad verdict must not lose the run's trace
        return exc


def score(jobs: Sequence[Job], store: JsonlStore, config: JudgeConfig) -> Scored:
    """Judge every job that has a stored trace and all its verdicts. No model calls."""
    outcomes: list[Outcome] = []
    unjudged = 0
    for job in jobs:
        record = store.get(job.key)
        if record is None:
            continue
        found = judge.verdicts_for(job, record.trace, store, config)
        if found is None:
            unjudged += 1
            continue
        outcomes.append(_outcome(job, record.trace, found))
    return Scored(outcomes, unjudged)


def _outcome(job: Job, trace: Trace, found: dict[str, tuple[bool, str]]) -> Outcome:
    verdicts = {rubric: passed for rubric, (passed, _) in found.items()}
    failed = []
    results = []
    for check in job.case.checks:
        passed = check.passes(trace, verdicts)
        results.append((check.describe(), passed))
        if passed:
            continue
        reason = f" ({found[check.rubric][1]})" if isinstance(check, checks.Judge) else ""
        failed.append(check.describe() + reason)
    skill = job.variant.skill
    triggered = None if skill is None else skill.name in trace.skills_invoked
    return Outcome(job.variant.id, job.case.id, job.sample, not failed, tuple(failed), triggered,
                   tuple(results))


def _parallel(items: Sequence[Item], work: Callable[[Item], Result],
              workers: int) -> Iterator[tuple[Item, Result | Exception]]:
    """Yield each item with its result, or the exception it raised, as each finishes."""
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(work, item): item for item in items}
        for future in as_completed(futures):
            try:
                yield futures[future], future.result()
            except Exception as exc:  # one failed call must not stop the others
                yield futures[future], exc
