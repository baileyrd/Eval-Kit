"""Leave-one-out ablation: the subject with one piece taken out, per piece.

A prompt's pieces are its sections, its examples (as one piece) and each
document. A skill's pieces are the ``##`` sections of its SKILL.md body and each
resource file. Comparing each ablated variant with the full one shows what each
piece is worth: a large drop means the piece carries its weight, no change means
it may be dead text.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from evalkit import frontmatter
from evalkit.spec import SKILL_FILE, PromptSpec, SkillSpec, Subject, Variant, load_skill_dir

_HEADING = re.compile(r"^#{1,2} ")
_FENCE = re.compile(r"^\s*(```|~~~)")
_NOT_PIECES = ("evals/",)  # never installed with the skill, so never a piece


@dataclass(frozen=True)
class Piece:
    kind: str  # "section", "examples", "document" or "resource"
    name: str  # section heading or name, document number, or resource path

    @property
    def label(self) -> str:
        return self.kind if self.kind == "examples" else f"{self.kind} {self.name}"

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.label.lower()).strip("-")


def pieces(subject: Subject) -> list[Piece]:
    """Every piece of *subject* that can be left out on its own."""
    if isinstance(subject, PromptSpec):
        out = [Piece("section", s.name) for s in subject.sections]
        out += [Piece("examples", "")] if subject.examples else []
        return out + [Piece("document", str(i)) for i in range(1, len(subject.documents) + 1)]
    out = [Piece("section", heading) for heading, _, _ in _skill_sections(subject.document.body)]
    return out + [Piece("resource", r.path) for r in subject.resources
                  if not r.path.startswith(_NOT_PIECES)]


def variants(variant: Variant, workdir: Path) -> list[tuple[Piece, Variant]]:
    """One variant per piece of *variant*'s subject, each missing that piece.

    Skills are rewritten as copies under *workdir*, replaced on every call.
    """
    subject = variant.subject
    if subject is None:
        raise ValueError(f"variant {variant.id!r} has no subject to ablate")
    out = []
    for piece in pieces(subject):
        if isinstance(subject, PromptSpec):
            ablated: Subject = without_prompt_piece(subject, piece)
        else:
            ablated = _without_skill_piece(subject, piece, workdir / piece.slug)
        out.append((piece, replace(variant, id=f"{variant.id}~{piece.slug}", subject=ablated)))
    return out


def without_prompt_piece(prompt: PromptSpec, piece: Piece) -> PromptSpec:
    if piece.kind == "section":
        return replace(prompt, sections=tuple(s for s in prompt.sections if s.name != piece.name))
    if piece.kind == "examples":
        return replace(prompt, examples=())
    index = int(piece.name) - 1
    return replace(prompt, documents=prompt.documents[:index] + prompt.documents[index + 1:])


def without_section(skill_md: str, heading: str) -> str:
    """*skill_md* with the ``##`` section titled *heading* cut from its body."""
    lines = skill_md.splitlines()
    first = frontmatter.parse(skill_md).body_start_line - 1
    for title, start, end in _skill_sections("\n".join(lines[first:])):
        if title == heading:
            return "\n".join(lines[:first + start] + lines[first + end:]) + "\n"
    raise ValueError(f"no section {heading!r}")


def _without_skill_piece(skill: SkillSpec, piece: Piece, workdir: Path) -> SkillSpec:
    target = workdir / skill.dir_name
    shutil.rmtree(workdir, ignore_errors=True)
    shutil.copytree(skill.root, target, ignore=shutil.ignore_patterns("evals", ".*"))
    if piece.kind == "resource":
        (target / piece.name).unlink()
    else:
        skill_md = target / SKILL_FILE
        text = skill_md.read_text(encoding="utf-8")
        skill_md.write_text(without_section(text, piece.name), encoding="utf-8")
    loaded = load_skill_dir(target, skill.targets)
    return replace(loaded, model=skill.model, effort=skill.effort)


def _skill_sections(body: str) -> list[tuple[str, int, int]]:
    """Each ``##`` section as (heading, first line, line after it), skipping code fences.
    A section runs until the next ``#`` or ``##`` heading."""
    starts: list[tuple[str, int]] = []
    bounds: list[int] = []
    fenced = False
    for number, line in enumerate(body.split("\n")):
        if _FENCE.match(line):
            fenced = not fenced
        if fenced or not _HEADING.match(line):
            continue
        bounds.append(number)
        if line.startswith("## "):
            starts.append((line[3:].strip(), number))
    end_of_body = len(body.split("\n"))
    out = []
    for heading, start in starts:
        end = next((b for b in bounds if b > start), end_of_body)
        out.append((heading, start, end))
    return out
