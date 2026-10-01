"""Tool output size for a fixed set of calls: what each response costs in Claude's context.

    uv run python eval/output_size.py [--sections] > eval/results/output-size-<label>.txt

Tokens are estimated at 2 chars/token (measured for GAMS-heavy tool output in the 2026-09-26 smoke session;
prose runs closer to 4, so this errs high). Free, local, no LLM.
"""

from __future__ import annotations

import re
import sys

from remind_rag import server

CALLS = [
    ("get_symbol", "vm_deltaCap"),
    ("get_symbol", "vm_cap"),
    ("get_symbol", "pm_taxCO2eqSum"),
    ("get_symbol", "vm_co2eq"),
    ("get_symbol", "q21_taxrevGHG"),
    ("get_links", "pm_taxCO2eqSum"),
    ("get_links", "vm_cap"),
    ("get_module", "45_carbonprice"),
    ("get_module", "33_carbonRemoval"),
    ("get_module", "15_climate"),
    ("get_switch", "cm_emiscen"),
    ("get_scenario", "SSP2-PkBudg1000"),
    ("search", "how is the carbon price turned into tax revenue"),
    ("search", "early retirement of capacities"),
    ("search", "why does the model not converge"),
]


def sections(text: str) -> list[tuple[str, int]]:
    """(heading, chars) for each '#'-heading or 'Label:' block, to see where the bulk is."""
    out, title, size = [], "(top)", 0
    for line in text.splitlines(keepends=True):
        if re.match(r"#{1,4} |\*\*[^*]+\*\*$|[A-Z][\w /()-]{2,40}:\s*$", line.strip()):
            out.append((title, size))
            title, size = line.strip()[:60], 0
        size += len(line)
    out.append((title, size))
    return [(t, s) for t, s in out if s]


def main() -> None:
    show = "--sections" in sys.argv
    total = 0
    print(f"index: {server.idx.meta.get('built_at')}  ({server._default_db().name})")
    print(f"{'call':48s} {'chars':>7s} {'lines':>6s} {'~tokens':>8s}")
    for tool, arg in CALLS:
        text = getattr(server, tool)(arg)
        total += len(text)
        print(f"{tool + '(' + arg[:36] + ')':48s} {len(text):7d} {text.count(chr(10)) + 1:6d} {len(text) // 2:8d}")
        if show:
            for title, size in sorted(sections(text), key=lambda x: -x[1])[:6]:
                print(f"    {size:7d}  {title}")
    print(f"{'TOTAL':48s} {total:7d} {'':6s} {total // 2:8d}")


if __name__ == "__main__":
    main()
