"""Expand a suite into jobs and give each a content key. Pure."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from evalkit import render
from evalkit.spec import Case, PromptSpec, SkillSpec, Suite, Variant

# Files under a skill that don't change how it behaves: its own eval suite and
# results. Editing a case must not invalidate every stored trace of the skill.
_NOT_SKILL = ("evals/", ".")


@dataclass(frozen=True)
class Job:
    variant: Variant
    case: Case
    sample: int
    key: str


def jobs(suite: Suite) -> list[Job]:
    """Every (variant, case, sample) in the suite, in a stable order."""
    out: list[Job] = []
    for variant in suite.variants:
        digest = subject_digest(variant)
        for case in suite.cases:
            for sample in range(suite.samples):
                out.append(Job(variant, case, sample, job_key(suite, variant, digest, case, sample)))
    return out


def job_key(suite: Suite, variant: Variant, digest: str, case: Case, sample: int) -> str:
    """Hash of every input that can change the run's result.

    The Claude CLI version is deliberately not part of the key: it is stored with
    each trace, and ``run`` re-runs a trace made by a different version.
    """
    payload: dict[str, object] = {
        "subject": digest,
        "model": variant.model,
        "effort": variant.effort,
        "allowed_tools": suite.allowed_tools,
        "input": render.user_turn(variant, case),
        "files": case.files,
        "sample": sample,
    }
    if case.checkout is not None:  # its source is a local path, so only the commits count
        payload["checkout"] = [case.checkout.head, case.checkout.base, case.checkout.ref]
    if suite.settings is not None:
        payload["settings"] = suite.settings
    return _sha(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode())


def subject_digest(variant: Variant) -> str:
    subject = variant.subject
    if subject is None:
        return "control"
    if isinstance(subject, PromptSpec):
        return _sha(json.dumps([render.system_prompt(subject), subject.tools]).encode())
    return _tree_digest(subject)


def _tree_digest(skill: SkillSpec) -> str:
    """Hash of every file in the skill directory that affects behaviour."""
    digest = hashlib.sha256()
    for path in sorted(p for p in skill.root.rglob("*") if p.is_file()):
        rel = path.relative_to(skill.root).as_posix()
        if rel.startswith(_NOT_SKILL) or "/." in f"/{rel}":
            continue
        digest.update(rel.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
