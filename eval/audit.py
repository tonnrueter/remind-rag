"""Audit rounds: ask the audit questions to a headless Sonnet, record answers + tool calls, grade with Opus.

    uv run python eval/audit.py run    --round 2 [--arms context+rag,context] [--ids Q1,Q5] [--grade] [--jobs 2]
    uv run python eval/audit.py grade  --round 2 [--arms ...] [--ids ...] [--force]
    uv run python eval/audit.py show   --round 2 Q5
    uv run python eval/audit.py report --round 2
    uv run python eval/audit.py import-answer --round 1 --id Q1 --arm interactive --file answer.md

Answerer: `claude -p` in the REMIND checkout, built-in tools limited to Read/Grep/Glob, `--setting-sources user`
(no project/parent CLAUDE.md), remind-context injected via --append-system-prompt-file (all arms named
"context*"), the remind-rag MCP server for arms named "*rag". Questions are asked verbatim.
Grader: `claude -p --model opus` in the same checkout, Read/Grep/Glob only, no MCP, blind to the arm, structured
output per `audit/grade_schema.json`, rubric `audit/rubric.md`.
Everything lands in eval/rounds/round-NN/{answers,grades}/<Q>-<arm>.json; `report` renders eval/rounds/round-NN.md.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from e2e_eval import HERE, RAG_DIR, mcp_config, static_context

AUDIT = HERE / "audit"
ROUNDS = HERE / "rounds"
QUESTIONS = {q["id"]: q for q in yaml.safe_load((AUDIT / "questions.yaml").read_text(encoding="utf-8"))}
ARMS = {"context+rag": (True, True), "context": (False, True), "rag": (True, False), "baseline": (False, False)}
DEFAULT_ARMS = "context+rag,context"
DEFAULT_REMIND = RAG_DIR.parent / "remind"
DEFAULT_DB = RAG_DIR / "data" / "remind-bge.db"
BUILTIN_TOOLS = "Read,Grep,Glob"
_print_lock = threading.Lock()


def say(*a) -> None:
    with _print_lock:
        print(*a, flush=True)


def round_dir(n: int) -> Path:
    return ROUNDS / f"round-{n:02d}"


def qkey(qid: str) -> int:
    return int(qid.lstrip("Q"))


_config_lock = threading.Lock()
_MCP_FILES: dict = {}  # db -> config file, written once per process


def claude_cmd(model: str, output_format: str, remind: Path, db: Path | None, context: bool,
               budget: float) -> list[str]:
    # the helper files are rewritten per call; the lock keeps parallel workers from reading half-written files
    with _config_lock:
        mcp_file = _MCP_FILES.get(db) or _MCP_FILES.setdefault(db, mcp_config(db))
        ctx_file = HERE / "results" / "static-context.md"
        if context:
            text = static_context(remind)
            if not ctx_file.exists() or ctx_file.read_text(encoding="utf-8") != text:
                ctx_file.write_text(text, encoding="utf-8")
    cmd = [shutil.which("claude"), "-p", "--model", model, "--output-format", output_format,
           "--setting-sources", "user", "--no-session-persistence", "--tools", BUILTIN_TOOLS,
           "--strict-mcp-config", "--mcp-config", str(mcp_file),
           "--allowedTools", BUILTIN_TOOLS + (",mcp__remind-rag" if db else ""),
           "--max-budget-usd", str(budget)]
    if output_format == "stream-json":
        cmd.append("--verbose")
    if context:
        cmd += ["--append-system-prompt-file", str(ctx_file)]
    return cmd


def git_head(path: Path) -> str:
    try:
        sha = subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True,
                               text=True).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "?"


def write_meta(rdir: Path, args) -> None:
    import sqlite3

    meta_file = rdir / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {"runs": []}
    db_meta = dict(sqlite3.connect(args.db).execute("SELECT key, value FROM meta").fetchall())
    meta["runs"].append({
        "started": time.strftime("%Y-%m-%d %H:%M:%S"), "arms": args.arms, "ids": args.ids,
        "answer_model": args.model, "grader_model": args.grader_model,
        "rag_git": git_head(RAG_DIR), "remind_git": git_head(args.remind),
        "remind_context_git": git_head(args.remind / "remind-context"),
        "db": str(args.db), "db_meta": db_meta,
    })
    meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- answering

def parse_stream(stdout: str) -> dict:
    out = {"tool_calls": [], "model": None}
    result = None
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            out["model"] = ev.get("model")
            out["mcp_servers"] = [s.get("name") for s in ev.get("mcp_servers", [])]
        elif ev.get("type") == "assistant":
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "tool_use":
                    inp = json.dumps(c.get("input", {}), ensure_ascii=False)
                    out["tool_calls"].append({"name": c.get("name"), "input": inp[:400]})
        elif ev.get("type") == "result":
            result = ev
    if result is None:
        out["error"] = "no result event"
        return out
    usage = result.get("usage", {})
    counts: dict[str, int] = {}
    for t in out["tool_calls"]:
        counts[t["name"]] = counts.get(t["name"], 0) + 1
    out.update(
        answer=result.get("result", ""), cost_usd=result.get("total_cost_usd"), turns=result.get("num_turns"),
        duration_s=round(result.get("duration_ms", 0) / 1000, 1), is_error=result.get("is_error", False),
        input_tokens=usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0), tool_counts=counts,
    )
    return out


def answer(q: dict, arm: str, args) -> dict:
    use_rag, context = ARMS[arm]
    cmd = claude_cmd(args.model, "stream-json", args.remind, args.db if use_rag else None, context, args.budget)
    t = time.perf_counter()
    proc = subprocess.run(cmd, input=q["question"], cwd=args.remind, capture_output=True, text=True,
                          encoding="utf-8", timeout=1200)
    res = parse_stream(proc.stdout)
    res.update(id=q["id"], kind=q["kind"], arm=arm, question=q["question"], wall_s=round(time.perf_counter() - t, 1))
    if "error" in res or res.get("is_error"):
        res["stderr"] = (proc.stderr or "")[-2000:]
    return res


# --------------------------------------------------------------------------- grading

def grade_prompt(q: dict, answer_text: str) -> str:
    rubric = (AUDIT / "rubric.md").read_text(encoding="utf-8")
    trap = " (TRAP: the question contains a false premise on purpose)" if q["kind"] == "trap" else ""
    return (f"{rubric}\n\n---\n\n## Question {q['id']}{trap}\n\n{q['question']}\n\n"
            f"## Answer key (human reviewer, 2026-09-24)\n\n{q['answer_key']}\n\n"
            f"## Answer to grade\n\n{answer_text}\n")


def grade(q: dict, answer_text: str, args) -> dict:
    schema = json.dumps(json.loads((AUDIT / "grade_schema.json").read_text(encoding="utf-8")))
    cmd = claude_cmd(args.grader_model, "json", args.remind, None, False, args.grader_budget)
    cmd += ["--json-schema", schema]
    t = time.perf_counter()
    proc = subprocess.run(cmd, input=grade_prompt(q, answer_text), cwd=args.remind, capture_output=True,
                          text=True, encoding="utf-8", timeout=1800)
    try:
        res = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": (proc.stderr or proc.stdout)[-2000:], "wall_s": round(time.perf_counter() - t, 1)}
    out = {"grade": res.get("structured_output"), "grader_cost_usd": res.get("total_cost_usd"),
           "grader_turns": res.get("num_turns"), "grader_duration_s": round(res.get("duration_ms", 0) / 1000, 1),
           "grader_model": next(iter(res.get("modelUsage", {}) or {}), args.grader_model)}
    if not out["grade"]:
        out["error"] = f"no structured output: {str(res.get('result'))[:1000]}"
    return out


def grade_one(rdir: Path, qid: str, arm: str, args) -> dict | None:
    a_file, g_file = rdir / "answers" / f"{qid}-{arm}.json", rdir / "grades" / f"{qid}-{arm}.json"
    if not a_file.exists():
        return None
    if g_file.exists() and not args.force:
        return json.loads(g_file.read_text(encoding="utf-8"))
    ans = json.loads(a_file.read_text(encoding="utf-8"))
    if not ans.get("answer"):
        say(f"{qid:4s} {arm:12s} grade skipped: no answer")
        return None
    g = grade(QUESTIONS[qid], ans["answer"], args)
    g.update(id=qid, arm=arm)
    if "error" in g:
        say(f"{qid:4s} {arm:12s} grade ERROR (not saved): {str(g['error'])[:200]}")
        return None
    g_file.parent.mkdir(parents=True, exist_ok=True)
    g_file.write_text(json.dumps(g, indent=2, ensure_ascii=False), encoding="utf-8")
    gr = g.get("grade") or {}
    say(f"{qid:4s} {arm:12s} graded  C{gr.get('critical', '?')} M{gr.get('major', '?')} m{gr.get('minor', '?')}  "
        f"{gr.get('failure_class', '')}  ${g.get('grader_cost_usd') or 0:.2f}" + ("  ERROR" if "error" in g else ""))
    return g


def process_question(rdir: Path, qid: str, arms: list[str], args) -> None:
    for arm in arms:
        a_file = rdir / "answers" / f"{qid}-{arm}.json"
        if a_file.exists() and not args.force:
            say(f"{qid:4s} {arm:12s} answer exists, kept")
        else:
            r = answer(QUESTIONS[qid], arm, args)
            if "error" in r or r.get("is_error") or not r.get("answer"):
                # not saved, so a rerun retries it (e.g. after hitting a usage limit)
                say(f"{qid:4s} {arm:12s} ERROR (not saved): {(r.get('answer') or r.get('error') or r.get('stderr') or '')[:200]}")
                continue
            a_file.write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
            say(f"{qid:4s} {arm:12s} answered ${r.get('cost_usd') or 0:.2f}  {r.get('turns')} turns  "
                f"{r.get('duration_s')} s  [{tool_summary(r)}]")
        if args.grade:
            grade_one(rdir, qid, arm, args)
    say(f"DONE {qid}")


# --------------------------------------------------------------------------- show / report

def load(rdir: Path, sub: str, qid: str, arm: str) -> dict:
    f = rdir / sub / f"{qid}-{arm}.json"
    if not f.exists():
        return {}
    d = json.loads(f.read_text(encoding="utf-8"))
    if sub == "grades":
        o = rdir / sub / f"{qid}-{arm}.override.json"
        if o.exists():
            d["grade"] = {**(d.get("grade") or {}), **json.loads(o.read_text(encoding="utf-8")), "overridden": True}
    return d


def arms_in(rdir: Path) -> list[str]:
    found = {f.stem.split("-", 1)[1] for f in (rdir / "answers").glob("Q*-*.json")}
    return sorted(found, key=lambda a: list(ARMS).index(a) if a in ARMS else 99)


def tool_summary(a: dict) -> str:
    c = a.get("tool_counts") or {}
    return ", ".join(f"{k.replace('mcp__remind-rag__', 'rag:')}×{v}" for k, v in c.items()) or "none"


def show(rdir: Path, qid: str) -> str:
    q = QUESTIONS[qid]
    lines = [f"{qid} ({q['kind']}): {q['question']}"]
    for arm in arms_in(rdir):
        a, g = load(rdir, "answers", qid, arm), (load(rdir, "grades", qid, arm).get("grade") or {})
        if not a:
            continue
        sev = f"C{g.get('critical', '?')} M{g.get('major', '?')} m{g.get('minor', '?')}" if g else "not graded"
        lines += [f"\n[{arm}] {sev} · {g.get('failure_class', '-')} · fix: {g.get('fix_layer', '-')}"
                  f" · premise: {g.get('premise_handled', '-')}",
                  f"  ${a.get('cost_usd') or 0:.2f}, {a.get('turns')} turns, {a.get('duration_s')} s; tools: {tool_summary(a)}"]
        if g.get("summary"):
            lines.append(f"  {g['summary']}")
        bad = [c for c in g.get("claims", []) if c.get("severity") in ("critical", "major")]
        for c in bad[:4]:
            lines.append(f"  - {c['severity'].upper()}: {c['claim']} → {c['note']} ({c['evidence']})")
        if g.get("answer_key_issues"):
            lines.append(f"  key issue: {g['answer_key_issues']}")
    return "\n".join(lines)


def md_cell(s) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def report(rdir: Path, n: int) -> Path:
    arms = arms_in(rdir)
    qids = sorted({f.stem.split("-", 1)[0] for f in (rdir / "answers").glob("Q*-*.json")}, key=qkey)
    meta = json.loads((rdir / "meta.json").read_text(encoding="utf-8")) if (rdir / "meta.json").exists() else {}
    last = (meta.get("runs") or [{}])[-1]
    out = [f"# Round {n:02d} (automated audit)", "",
           f"Generated by `eval/audit.py report` on {time.strftime('%Y-%m-%d %H:%M')}. Raw data: `round-{n:02d}/`. "
           "Grades by a headless Opus grader (`audit/rubric.md`), blind to the arm; human overrides are marked ✎.", "",
           "| | |", "|---|---|",
           f"| Questions | `audit/questions.yaml` ({len(qids)} asked) |",
           f"| Arms | {', '.join(arms)} |",
           f"| Answerer / grader | {last.get('answer_model', '?')} / {last.get('grader_model', '?')} |",
           f"| rag / REMIND / remind-context | {last.get('rag_git', '?')} / {last.get('remind_git', '?')} / "
           f"{last.get('remind_context_git', '?')} |",
           f"| Index | `{Path(last.get('db', '?')).name}` ({last.get('db_meta', {}).get('chunks', '?')} chunks, built "
           f"{last.get('db_meta', {}).get('built_at', '?')}) |", ""]

    out += ["## Totals", "", "| arm | graded | Critical | Major | Minor | premise corrected (traps) | mean cost | "
            "mean turns | mean time | RAG calls/answer | grading cost |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm in arms:
        ans = [load(rdir, "answers", q, arm) for q in qids]
        ans = [a for a in ans if a]
        grs = [load(rdir, "grades", q, arm).get("grade") for q in qids]
        grs = [g for g in grs if g]
        graw = [load(rdir, "grades", q, arm) for q in qids]
        traps = [(load(rdir, "grades", q, arm).get("grade") or {}) for q in qids if QUESTIONS[q]["kind"] == "trap"]
        traps = [t for t in traps if t]
        rag_calls = [sum(v for k, v in (a.get("tool_counts") or {}).items() if k.startswith("mcp__")) for a in ans]
        k = max(len(ans), 1)
        out.append(f"| {arm} | {len(grs)}/{len(ans)} | {sum(g.get('critical', 0) for g in grs)} | "
                   f"{sum(g.get('major', 0) for g in grs)} | {sum(g.get('minor', 0) for g in grs)} | "
                   f"{sum(t.get('premise_handled') == 'corrected' for t in traps)}/{len(traps)} | "
                   f"${sum(a.get('cost_usd') or 0 for a in ans) / k:.2f} | {sum(a.get('turns') or 0 for a in ans) / k:.1f} | "
                   f"{sum(a.get('duration_s') or 0 for a in ans) / k:.0f} s | {sum(rag_calls) / k:.1f} | "
                   f"${sum(g.get('grader_cost_usd') or 0 for g in graw if g):.2f} |")

    out += ["", "## Results", "", "| Q | kind | " + " | ".join(arms) + " |", "|---|---|" + "---|" * len(arms)]
    for q in qids:
        cells = []
        for arm in arms:
            g = load(rdir, "grades", q, arm).get("grade")
            cells.append("—" if not g else f"C{g.get('critical')} M{g.get('major')} m{g.get('minor')} · "
                         f"{md_cell(g.get('failure_class'))}" + (" ✎" if g.get("overridden") else ""))
        out.append(f"| [{q}](#{q.lower()}) | {QUESTIONS[q]['kind']} | " + " | ".join(cells) + " |")

    tally: dict[tuple[str, str], list[str]] = {}
    for q in qids:
        for arm in arms:
            g = load(rdir, "grades", q, arm).get("grade") or {}
            if g and g.get("failure_class") not in (None, "", "none"):
                tally.setdefault((g["failure_class"], g.get("fix_layer", "?")), []).append(f"{q}/{arm}")
    out += ["", "## Failure classes", "", "| class | fix layer | count | where |", "|---|---|---|---|"]
    for (cls, layer), where in sorted(tally.items(), key=lambda kv: -len(kv[1])):
        out.append(f"| {md_cell(cls)} | {layer} | {len(where)} | {', '.join(where)} |")

    for q in qids:
        out += ["", "---", "", f"## {q}", "", f"**Question ({QUESTIONS[q]['kind']}):** {QUESTIONS[q]['question']}"]
        for arm in arms:
            a, gd = load(rdir, "answers", q, arm), load(rdir, "grades", q, arm)
            if not a:
                continue
            g = gd.get("grade") or {}
            out += ["", f"### {arm}", "",
                    f"${a.get('cost_usd') or 0:.2f} · {a.get('turns')} turns · {a.get('duration_s')} s · "
                    f"tools: {tool_summary(a)}", "", "<details><summary>Answer</summary>", "",
                    "\n".join("> " + line for line in (a.get("answer") or "(no answer)").splitlines()), "", "</details>"]
            if not g:
                out += ["", "*Not graded.*" + (f" Error: {md_cell(gd.get('error'))[:300]}" if gd.get("error") else "")]
                continue
            out += ["", f"**C{g.get('critical')} M{g.get('major')} m{g.get('minor')}** · {g.get('failure_class')} · "
                    f"fix: {g.get('fix_layer')} · premise: {g.get('premise_handled')}"
                    + (" · ✎ overridden" if g.get("overridden") else ""), "", g.get("summary", ""), "",
                    "| # | claim | verdict | severity | evidence | note |", "|---|---|---|---|---|---|"]
            for i, c in enumerate(g.get("claims", []), 1):
                out.append(f"| {i} | {md_cell(c.get('claim'))} | {c.get('verdict')} | {c.get('severity')} | "
                           f"{md_cell(c.get('evidence'))} | {md_cell(c.get('note'))} |")
            if g.get("went_well"):
                out += ["", f"*Went well:* {g['went_well']}"]
            if g.get("answer_key_issues"):
                out += ["", f"*Answer key:* {g['answer_key_issues']}"]
    path = ROUNDS / f"round-{n:02d}.md"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--round", type=int, required=True)
    common.add_argument("--remind", type=Path, default=DEFAULT_REMIND)
    common.add_argument("--db", type=Path, default=DEFAULT_DB)
    common.add_argument("--arms", default=DEFAULT_ARMS)
    common.add_argument("--ids", default=",".join(QUESTIONS))
    common.add_argument("--model", default="sonnet")
    common.add_argument("--grader-model", default="opus")
    common.add_argument("--budget", type=float, default=2.0, help="max USD per answer")
    common.add_argument("--grader-budget", type=float, default=5.0, help="max USD per grade")
    common.add_argument("--force", action="store_true", help="redo existing answers/grades")
    p = sub.add_parser("run", parents=[common])
    p.add_argument("--grade", action="store_true", help="grade each answer right after it")
    p.add_argument("--jobs", type=int, default=2, help="questions processed in parallel")
    sub.add_parser("grade", parents=[common])
    p = sub.add_parser("show", parents=[common])
    p.add_argument("qid")
    sub.add_parser("report", parents=[common])
    p = sub.add_parser("import-answer", parents=[common], help="store an externally obtained answer for grading")
    p.add_argument("--id", required=True)
    p.add_argument("--arm", required=True)
    p.add_argument("--file", type=Path, required=True)
    args = ap.parse_args()
    args.remind, args.db = args.remind.resolve(), args.db.resolve()
    rdir = round_dir(args.round)
    ids = sorted(args.ids.split(","), key=qkey)
    arms = args.arms.split(",")
    unknown = [a for a in arms if a not in ARMS] if args.cmd == "run" else []
    if unknown or any(i not in QUESTIONS for i in ids):
        sys.exit(f"unknown arm or question id: {unknown or ids}")

    if args.cmd == "run":
        (rdir / "answers").mkdir(parents=True, exist_ok=True)
        write_meta(rdir, args)
        t = time.perf_counter()
        with ThreadPoolExecutor(args.jobs) as ex:
            for f in [ex.submit(process_question, rdir, q, arms, args) for q in ids]:
                f.result()
        say(f"ALL DONE in {(time.perf_counter() - t) / 60:.0f} min")
    elif args.cmd == "grade":
        for q in ids:
            for arm in arms:
                grade_one(rdir, q, arm, args)
    elif args.cmd == "show":
        print(show(rdir, args.qid))
    elif args.cmd == "report":
        print(f"wrote {report(rdir, args.round)}")
    elif args.cmd == "import-answer":
        (rdir / "answers").mkdir(parents=True, exist_ok=True)
        q = QUESTIONS[args.id]
        rec = {"id": args.id, "kind": q["kind"], "arm": args.arm, "question": q["question"],
               "answer": args.file.read_text(encoding="utf-8"), "imported_from": str(args.file)}
        (rdir / "answers" / f"{args.id}-{args.arm}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False),
                                                                    encoding="utf-8")
        print(f"stored {args.id}-{args.arm}")


if __name__ == "__main__":
    main()
