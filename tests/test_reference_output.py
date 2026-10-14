"""Safety net for refactoring (#40): the output of a fixed set of tool calls (the 15 of eval/output_size.py) must
stay byte for byte as recorded in tests/reference/. A deliberate output change regenerates the files in the same
commit, and the commit message says why:

    uv run python tests/test_reference_output.py --update

Recorded with index v8 (2026-10-14); a rebuild that changes tool output shows up here too.
"""

from __future__ import annotations

import difflib
import importlib.util
import re
import sys
from pathlib import Path

import pytest

from remind_rag import server

HERE = Path(__file__).parent
REFERENCE = HERE / "reference"
_spec = importlib.util.spec_from_file_location("output_size", HERE.parent / "eval" / "output_size.py")
_output_size = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_output_size)
CALLS: list[tuple[str, str]] = _output_size.CALLS


def reference_file(tool: str, arg: str) -> Path:
    return REFERENCE / f"{tool}-{re.sub(r'\W+', '_', arg).strip('_')[:60]}.txt"


def call(tool: str, arg: str) -> str:
    return getattr(server, tool)(arg)


@pytest.mark.parametrize("tool, arg", CALLS)
def test_output_unchanged(tool, arg):
    path = reference_file(tool, arg)
    assert path.exists(), f"no reference for {tool}({arg!r}); run: uv run python tests/test_reference_output.py --update"
    expected, actual = path.read_text(encoding="utf-8"), call(tool, arg)
    if actual != expected:
        diff = "".join(difflib.unified_diff(expected.splitlines(keepends=True), actual.splitlines(keepends=True),
                                            "reference", "now", n=1))
        pytest.fail(f"{tool}({arg!r}) changed; if on purpose: uv run python tests/test_reference_output.py "
                    f"--update\n{diff[:4000]}")


if __name__ == "__main__":
    if sys.argv[1:] != ["--update"]:
        sys.exit("usage: uv run python tests/test_reference_output.py --update")
    REFERENCE.mkdir(exist_ok=True)
    for tool, arg in CALLS:
        reference_file(tool, arg).write_text(call(tool, arg), encoding="utf-8", newline="\n")
    print(f"wrote {len(CALLS)} files to {REFERENCE} (index: {server._default_db()})")
