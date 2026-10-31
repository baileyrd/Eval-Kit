"""Check out a past revision of a file's repository, to run a variant as it was."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class GitError(RuntimeError):
    """git could not produce the revision asked for."""


@contextmanager
def at_revision(path: Path, rev: str) -> Iterator[Path]:
    """Yield where *path* is in a detached worktree at *rev*; remove it afterwards."""
    path = path.resolve()
    top = Path(_git(path.parent, "rev-parse", "--show-toplevel"))
    with tempfile.TemporaryDirectory(prefix="evalkit-rev-") as tmp:
        tree = Path(tmp) / "tree"
        _git(top, "worktree", "add", "--detach", str(tree), rev)
        try:
            yield tree / path.relative_to(top)
        finally:
            _git(top, "worktree", "remove", "--force", str(tree))


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()
