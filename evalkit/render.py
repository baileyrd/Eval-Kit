"""Turn a variant and a case into the text the model sees. Pure."""

from __future__ import annotations

from evalkit.spec import Case, PromptSpec, Variant


def system_prompt(prompt: PromptSpec) -> str:
    """System sections as XML-tagged blocks, then the examples."""
    parts = [_tag(s.name, s.text) for s in prompt.sections if s.placement == "system"]
    if prompt.examples:
        examples = "\n".join(
            _tag("example", _tag("input", e.input) + "\n" + _tag("output", e.output))
            for e in prompt.examples
        )
        parts.append(_tag("examples", examples))
    return "\n\n".join(parts)


def user_turn(variant: Variant, case: Case) -> str:
    """The user message: documents first, then user sections, then the query."""
    prompt = variant.subject
    if not isinstance(prompt, PromptSpec):
        return case.input
    parts: list[str] = []
    if prompt.documents:
        documents = "\n".join(
            f'<document index="{i}">\n{_tag("document_content", text)}\n</document>'
            for i, text in enumerate(prompt.documents, start=1)
        )
        parts.append(_tag("documents", documents))
    parts += [_tag(s.name, s.text) for s in prompt.sections if s.placement == "user"]
    parts.append(prompt.template.replace("{{input}}", case.input))
    return "\n\n".join(parts)


def _tag(name: str, text: str) -> str:
    return f"<{name}>\n{text.strip()}\n</{name}>"
