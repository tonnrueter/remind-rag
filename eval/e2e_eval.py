"""End-to-end: headless Claude Code on eval questions, baseline vs. with the remind-rag MCP server.

    uv run python eval/e2e_eval.py --remind ../remind [--model sonnet] [--ids sw-startyear,con-EWdecay]

Both arms run in the REMIND checkout with read-only tools (Read/Grep/Glob) and whatever CLAUDE.md/AGENT.md
context Claude Code normally loads; the RAG arm additionally gets the MCP tools. Results are written to
eval/results/e2e-<timestamp>.json; correctness is graded afterwards against the reference answers.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
RAG_DIR = HERE.parent
QUESTIONS = yaml.safe_load((HERE / "questions.yaml").read_text(encoding="utf-8"))
DEFAULT_IDS = ["sym-emiCdrTeDetail", "sym-capEarlyReti", "sw-EWcropland", "sw-iterTargetAdj",
               "real-functionalForm", "xref-co2captureCdr", "xref-scc", "con-dacEnergy", "con-EWdecay",
               "doc-restart"]
# (name, use MCP index, inject static remind-context)
ARMS = [("baseline", False, False), ("rag", True, False), ("context", False, True), ("context+rag", True, True)]
PROMPT_SUFFIX = ("\n\nAnswer concisely (max ~120 words) and cite file paths with line numbers. "
                 "Do not modify any files.")


def mcp_config(db: Path | None) -> Path:
    servers = {}
    if db:
        servers["remind-rag"] = {
            "command": "uv",
            "args": ["run", "--offline", "--directory", str(RAG_DIR), "python", "-m", "remind_rag.server"],
            "env": {"REMIND_RAG_DB": str(db.resolve())},
        }
    path = HERE / "results" / ("mcp-rag.json" if db else "mcp-none.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def static_context(remind: Path) -> str:
    """remind-context/AGENT.md with its @-imports expanded, i.e. what a CLAUDE.md pointer would load."""
    base = remind / "remind-context"
    lines = []
    for line in (base / "AGENT.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("@") and (base / line[1:].strip()).is_file():
            lines.append((base / line[1:].strip()).read_text(encoding="utf-8"))
        else:
            lines.append(line)
    return "\n".join(lines)


def run_one(question: str, remind: Path, model: str, db: Path | None, context: bool = False) -> dict:
    tools = "Read,Grep,Glob" + (",mcp__remind-rag" if db else "")
    # prompt goes via stdin: several flags are variadic and would swallow a trailing positional argument
    cmd = [shutil.which("claude"), "-p", "--output-format", "json", "--model", model,
           "--strict-mcp-config", "--mcp-config", str(mcp_config(db)), "--allowedTools", tools,
           "--disallowedTools", "Bash,Edit,Write,WebFetch,WebSearch,Agent", "--no-session-persistence"]
    if context:
        # Claude Code does not read AGENT.md, so the static context is injected like a CLAUDE.md would be
        ctx_file = HERE / "results" / "static-context.md"
        ctx_file.write_text(static_context(remind), encoding="utf-8")
        cmd += ["--append-system-prompt-file", str(ctx_file)]
    t = time.perf_counter()
    proc = subprocess.run(cmd, input=question + PROMPT_SUFFIX, cwd=remind, capture_output=True, text=True,
                          encoding="utf-8", timeout=900)
    wall = time.perf_counter() - t
    try:
        res = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": (proc.stderr or proc.stdout)[-2000:], "wall_s": wall}
    usage = res.get("usage", {})
    return {
        "answer": res.get("result", ""),
        "cost_usd": res.get("total_cost_usd"),
        "turns": res.get("num_turns"),
        "duration_s": round(res.get("duration_ms", 0) / 1000, 1),
        "input_tokens": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "is_error": res.get("is_error", False),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remind", type=Path, required=True)
    ap.add_argument("--db", type=Path, default=RAG_DIR / "data" / "remind-bge-v0.db")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--ids", default=",".join(DEFAULT_IDS))
    ap.add_argument("--arms", default="baseline,rag,context,context+rag")
    args = ap.parse_args()

    ids = args.ids.split(",")
    qs = [q for q in QUESTIONS if q["id"] in ids]
    results = []
    for q in qs:
        for arm, db, ctx in ARMS:
            if arm not in args.arms.split(","):
                continue
            print(f"{q['id']:22s} {arm:11s} ...", end=" ", flush=True)
            r = run_one(q["question"], args.remind.resolve(), args.model, db and args.db, ctx)
            r.update(id=q["id"], type=q["type"], arm=arm, question=q["question"], reference=q["answer"])
            results.append(r)
            print(f"${r.get('cost_usd') or 0:.3f}  {r.get('turns')} turns  {r.get('duration_s')} s"
                  + ("  ERROR" if "error" in r or r.get("is_error") else ""), flush=True)

    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"e2e-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out}")

    for arm, _, _ in ARMS:
        rs = [r for r in results if r["arm"] == arm and "error" not in r]
        if rs:
            n = len(rs)
            print(f"{arm:11s} n={n}  mean cost ${sum(r['cost_usd'] or 0 for r in rs) / n:.3f}  "
                  f"mean turns {sum(r['turns'] or 0 for r in rs) / n:.1f}  "
                  f"mean time {sum(r['duration_s'] for r in rs) / n:.0f} s  "
                  f"mean input tokens {sum(r['input_tokens'] for r in rs) / n:,.0f}")


if __name__ == "__main__":
    main()
