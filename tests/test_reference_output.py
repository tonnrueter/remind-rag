"""Safety net for refactoring (#40): the output of a fixed set of tool calls (the 15 of eval/output_size.py), and
what Claude reads before calling any tool (server instructions, tool descriptions and parameters), must stay byte
for byte as recorded in tests/reference/. A deliberate output change regenerates the files in the same
commit, and the commit message says why:

    uv run python tests/test_reference_output.py --update

Recorded with index v8 (2026-10-14), unchanged on v9; a rebuild that changes tool output shows up here too.
"""

from __future__ import annotations

import asyncio
import difflib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from remind_rag import server, store

HERE = Path(__file__).parent
REFERENCE = HERE / "reference"
_spec = importlib.util.spec_from_file_location("output_size", HERE.parent / "eval" / "output_size.py")
_output_size = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_output_size)
CALLS: list[tuple[str, str]] = _output_size.CALLS


def reference_file(tool: str, arg: str) -> Path:
    slug = re.sub(r"\W+", "_", arg).strip("_")[:60]
    return REFERENCE / f"{tool}-{slug}.txt"


def call(tool: str, arg: str) -> str:
    return getattr(server, tool)(arg)


def described() -> dict[str, str]:
    """What a client reads at connection time: the instructions, and per tool its description and parameters."""
    tools = [
        {"name": t.name, "description": t.description, "parameters": t.input_schema}
        for t in asyncio.run(server.server.list_tools())
    ]
    return {
        "server-instructions.txt": server.server.instructions,
        "server-tools.json": json.dumps(tools, indent=1, ensure_ascii=False) + "\n",
    }


def compare(path: Path, actual: str, what: str) -> None:
    assert path.exists(), f"no reference for {what}; run: uv run python tests/test_reference_output.py --update"
    expected = path.read_text(encoding="utf-8")
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True), actual.splitlines(keepends=True), "reference", "now", n=1
            )
        )
        pytest.fail(
            f"{what} changed; if on purpose: uv run python tests/test_reference_output.py --update\n{diff[:4000]}"
        )


@pytest.mark.parametrize("tool, arg", CALLS)
def test_output_unchanged(tool, arg):
    compare(reference_file(tool, arg), call(tool, arg), f"{tool}({arg!r})")


@pytest.mark.parametrize("name", ["server-instructions.txt", "server-tools.json"])
def test_descriptions_unchanged(name):
    compare(REFERENCE / name, described()[name], name)


if __name__ == "__main__":
    if sys.argv[1:] != ["--update"]:
        sys.exit("usage: uv run python tests/test_reference_output.py --update")
    REFERENCE.mkdir(exist_ok=True)
    for tool, arg in CALLS:
        reference_file(tool, arg).write_text(call(tool, arg), encoding="utf-8", newline="\n")
    for name, text in described().items():
        (REFERENCE / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(CALLS) + 2} files to {REFERENCE} (index: {store.default_db()})")
