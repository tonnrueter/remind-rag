"""Select which REMIND files get indexed."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .checkout import tracked_files

INCLUDE_SUFFIXES = {".gms", ".r", ".rmd", ".md"}
INCLUDE_FILES = {"config/default.cfg"}
# data folders, vendored/generated stuff, and the existing static context (baseline)
EXCLUDE_DIRS = {".git", "renv", "remind-context", "output", "input", "doc", ".github"}


def iter_files(root: Path) -> Iterator[tuple[str, str]]:
    """Yield (relative posix path, text) for every file to index. In a git checkout only tracked files count, so
    the user's own notes, scratch scripts and logs in the working folder stay out of the index."""
    tracked = tracked_files(root)
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if any(part in EXCLUDE_DIRS for part in p.relative_to(root).parts[:-1]):
            continue
        if p.suffix.lower() not in INCLUDE_SUFFIXES and rel not in INCLUDE_FILES:
            continue
        if tracked is not None and rel not in tracked:
            continue
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        yield rel, text
