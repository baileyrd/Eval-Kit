# evalkit

Evaluate prompts and Agent Skills: statically against Anthropic's published
guidance, and behaviourally by running them through the Claude CLI and
comparing variants on the same cases.

Standard library only, Python 3.11+. See [docs/design.md](docs/design.md).

## Status

Phase 1 of 4: `lint` and `init`. Neither makes a model call. Running suites
(`run`, `score`, `compare`, `gate`) arrives in later phases; see the design.

## Use

```
python3 -m evalkit lint path/to/skill_pack            # every SKILL.md below it
python3 -m evalkit lint my.prompt.toml my.skill.toml  # subject files
python3 -m evalkit lint skills/ --min-severity warn --format json
python3 -m evalkit init path/to/skill                 # writes evals/suite.toml
```

`lint` exits 1 when any finding is an error. Each finding names the rule, the
file, and the Anthropic page the rule comes from.

## Subjects

A **skill** is a directory with a `SKILL.md`; that file stays canonical. A
`*.skill.toml` points at one and says where it must work:

```toml
schema  = 1
kind    = "skill"
from    = "../skill_pack/web_dev/datastar-pro"
targets = ["claude-code"]        # claude-code | claude.ai | api
```

A **prompt** is a `*.prompt.toml` holding only what the `claude` CLI can apply:
model, effort, tools, ordered sections rendered as XML tags, examples, and the
user turn. Messages API fields the CLI can't set (`max_tokens`, `temperature`,
`thinking`, ...) are rejected with the reason.

```toml
schema = 1
kind   = "prompt"
id     = "reviewer"
model  = "claude-opus-5-5"
effort = "high"

[[section]]
name = "instructions"
text = "file:reviewer/instructions.md"   # relative to this file, inside the repo

[[example]]
input  = "..."
output = "..."

[user]
template = "Review this change: {{input}}"
```

## Lint rules

| Rule | Checks |
|---|---|
| `skill.name` | 1–64 of `[a-z0-9-]`, matches its directory, no reserved words |
| `skill.description.limits` / `.style` | ≤1024 characters, no XML; third person, says when to use it |
| `skill.listing` | description + `when_to_use` ≤ 1,536 characters |
| `skill.portable` | Claude Code-only keys (error when `targets` excludes Claude Code); unknown keys |
| `skill.compatibility` / `skill.metadata` | ≤500 characters; metadata values are strings |
| `skill.body` | < 500 lines, < ~5k tokens (estimated) |
| `skill.refs` / `skill.paths` | links resolve, one level deep, table of contents past 100 lines; forward slashes |
| `skill.cases` | an eval suite with at least 3 cases |
| `prompt.examples` | 3–5 examples |
| `prompt.tone` | capitalised MUST/NEVER emphasis; rules stated only as negatives |

The limits live in `evalkit/rules.py` with the page each comes from and the
date it was last checked.

## Develop

```
python3 -m unittest discover -s tests -t .
python3 -m mypy        # strict; CI-only dependency
```
