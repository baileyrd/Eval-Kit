"""Materialise a case's git checkout, and expand ``${NAME}`` in machine-specific text.

The checkout is a new repository fetched by SHA from a local clone, never a worktree
of it: the run sees the head and its history, no later commits, and nothing that
leads back to the clone. Its ``origin`` is itself, so a fetch of ``ref`` from
``origin`` works and confirms the head.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from evalkit.spec import Checkout

# A local fetch by SHA needs the source's upload-pack to allow unadvertised objects.
_UPLOAD_PACK = "git -c uploadpack.allowAnySHA1InWant=true upload-pack"
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class CheckoutError(RuntimeError):
    """A variable is unset, or the clone could not provide the commits."""


def expand(text: str, env: dict[str, str] | None = None) -> str:
    """Replace each ``${NAME}`` with its environment value; an unset one is an error,
    because a silently empty path in a deny rule would deny nothing."""
    values = os.environ if env is None else env

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if not values.get(name):
            raise CheckoutError(f"${{{name}}} is not set")
        return values[name]

    return _VARIABLE.sub(value, text)


def materialise(checkout: Checkout, dest: Path) -> None:
    """Check out ``checkout.head`` into the empty directory *dest*."""
    source = Path(expand(checkout.source)).expanduser().resolve()
    if not source.is_dir():
        raise CheckoutError(f"checkout source {source} is not a directory")
    commits = [c for c in (checkout.base, checkout.head) if c]
    _git(dest, "init", "-q")
    _git(dest, "fetch", "-q", "--no-tags", "--upload-pack", _UPLOAD_PACK, str(source), *commits)
    _git(dest, "checkout", "-q", "--detach", checkout.head)
    if checkout.ref:
        _git(dest, "update-ref", checkout.ref, checkout.head)
    _git(dest, "remote", "add", "origin", str(dest.resolve()))


def _git(cwd: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise CheckoutError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()}")
