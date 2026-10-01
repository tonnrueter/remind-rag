"""Which REMIND checkout an index belongs to: the commit it was built from vs the checkout it is served for."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def git_head(root: Path) -> tuple[str, str] | None:
    """(commit, branch) of a checkout, read-only; None if root is no git checkout or git is missing."""
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True,
                              timeout=10).stdout.strip()

    try:
        return git("rev-parse", "HEAD"), git("rev-parse", "--abbrev-ref", "HEAD")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def served_root(meta: dict[str, str]) -> Path | None:
    """REMIND_RAG_ROOT if set, else the root the index was built from; None if it doesn't exist on this machine."""
    root = Path(os.environ.get("REMIND_RAG_ROOT") or meta["root"])
    return root.resolve() if root.is_dir() else None


def staleness(meta: dict[str, str], root: Path | None) -> str:
    """One sentence for the server instructions when the index may not match the checkout; '' when it does."""
    built = meta.get("remind_commit")
    head = git_head(root) if root else None
    hint = "If a result doesn't match the file, trust the file."
    if not built:
        return (f"This index doesn't record which REMIND commit it was built from (built {meta.get('built_at')}). "
                + hint)
    if head is None:
        return f"This index was built from REMIND commit {built[:9]}; the checkout's commit is unknown. " + hint
    if head[0] != built:
        return (f"This index was built from REMIND commit {built[:9]} ({meta.get('remind_branch')}), the checkout "
                f"is at {head[0][:9]} ({head[1]}): line numbers and code may differ. " + hint)
    return ""


def tracked_files(root: Path) -> set[str] | None:
    """Paths git tracks under root (posix, relative); None if root is no git checkout or git is missing."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True,
                             timeout=60).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return {p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p}
