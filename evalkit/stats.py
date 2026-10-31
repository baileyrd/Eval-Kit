"""Paired comparison of two variants over the cases both have results for. Pure.

- Per-case rate: the share of a case's samples that passed.
- Lift: the mean over shared cases of (head rate - base rate).
- Interval: a 95% percentile bootstrap that resamples cases, with a fixed seed.
- Flipped case: one whose majority verdict differs between the two variants.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from evalkit.engine import Outcome

DRAWS = 2000
SEED = 0
CONFIDENCE = 0.95


@dataclass(frozen=True)
class Summary:
    variant: str
    runs: int
    pass_rate: float
    trigger_rate: float | None  # None when the variant has no skill


@dataclass(frozen=True)
class Comparison:
    base: str
    head: str
    cases: int
    lift: float
    low: float
    high: float
    improved: tuple[str, ...]  # cases flipped from fail to pass
    regressed: tuple[str, ...]  # cases flipped from pass to fail
    lift_when_triggered: float | None  # head limited to runs where its skill loaded


def summarise(outcomes: Iterable[Outcome]) -> list[Summary]:
    by_variant: dict[str, list[Outcome]] = defaultdict(list)
    for outcome in outcomes:
        by_variant[outcome.variant].append(outcome)
    out = []
    for variant, runs in by_variant.items():
        triggers = [o.triggered for o in runs if o.triggered is not None]
        out.append(Summary(variant, len(runs), _mean([o.passed for o in runs]),
                           _mean(triggers) if triggers else None))
    return out


def compare(outcomes: Sequence[Outcome], base: str, head: str) -> Comparison:
    base_rates = _case_rates(o for o in outcomes if o.variant == base)
    head_rates = _case_rates(o for o in outcomes if o.variant == head)
    shared = sorted(base_rates.keys() & head_rates.keys())
    if not shared:
        raise ValueError(f"{base!r} and {head!r} have no case in common with stored results")
    diffs = [head_rates[c] - base_rates[c] for c in shared]
    low, high = bootstrap(diffs)
    triggered_rates = _case_rates(o for o in outcomes if o.variant == head and o.triggered)
    when_triggered = [triggered_rates[c] - base_rates[c] for c in shared if c in triggered_rates]
    has_skill = any(o.triggered is not None for o in outcomes if o.variant == head)
    return Comparison(
        base, head, len(shared), _mean(diffs), low, high,
        improved=tuple(c for c in shared if base_rates[c] < 0.5 <= head_rates[c]),
        regressed=tuple(c for c in shared if head_rates[c] < 0.5 <= base_rates[c]),
        lift_when_triggered=_mean(when_triggered) if has_skill and when_triggered else None,
    )


def bootstrap(diffs: Sequence[float], draws: int = DRAWS, seed: int = SEED) -> tuple[float, float]:
    """Percentile interval of the mean of *diffs*, resampling with replacement."""
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(rng.choices(diffs, k=n)) / n for _ in range(draws))
    tail = (1 - CONFIDENCE) / 2
    return means[int(tail * draws)], means[min(draws - 1, int((1 - tail) * draws))]


def _case_rates(outcomes: Iterable[Outcome]) -> dict[str, float]:
    by_case: dict[str, list[bool]] = defaultdict(list)
    for outcome in outcomes:
        by_case[outcome.case].append(outcome.passed)
    return {case: _mean(passes) for case, passes in by_case.items()}


def _mean(values: Sequence[bool | float]) -> float:
    return sum(values) / len(values) if values else 0.0
