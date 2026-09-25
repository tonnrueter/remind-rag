---
name: audit-round
description: Run a paid audit round of the REMIND RAG (headless Sonnet answers in context+rag and context arms, blind Opus grading) with a short per-question overview in chat, then render the round report. Use when the user asks to run, resume or report an audit / evaluation round.
---

# Audit round

Paid: in round 02 (20 questions × 2 arms) it cost ~$4 for answers + ~$7 for grading, 16 min. State the expected
cost once before starting; the user manages their limits.

## Before the run
1. Check that answer keys flagged in the previous round are fixed (`eval/audit/questions.yaml`) and note what
   changed in the RAG since then (git log, `FINDINGS.md`).
2. New grader or rubric? Calibrate first: `import-answer` the hand-graded answers from `eval/rounds/round-01.md`
   (Q1, Q18) into a scratch round and `grade` them; the verdicts should be close to the manual ones
   (Q1: 0/4/2, Q18: 0/0/2).
3. Pilot 2 questions (one trap): `uv run python eval/audit.py run --round N --ids Q1,Q5 --grade`.
   Check the answer JSON: the `context+rag` arm lists `mcp__remind-rag__*` tool calls and `mcp_servers:
   ['remind-rag']`, and the `context` arm has none.

## The run
```powershell
uv run python eval/audit.py run --round N --grade --jobs 2    # run_in_background
```
Watch the output with a Monitor filtered to `DONE|ERROR|Traceback`. After each `DONE Qn`, run
`uv run python eval/audit.py show --round N Qn` and post a short overview: both arms' Critical/Major/Minor, the
main failure, tool use and cost, anything notable about the answer key or REMIND itself.

Failed answers (e.g. a usage limit) aren't saved; rerunning the same command resumes.

## After the run
1. `uv run python eval/audit.py report --round N` → `eval/rounds/round-NN.md`.
2. Summarize totals per arm, failure classes with their fix layer, answer-key issues, and suspected REMIND bugs
   (add them to `../TODO.md` #30, unverified).
3. The user reviews the report; disagreements go into `eval/rounds/round-NN/grades/<Q>-<arm>.override.json`.
4. Update `FINDINGS.md` and `../TODO.md`.
