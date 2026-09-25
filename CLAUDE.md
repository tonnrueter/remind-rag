# remind-rag: project notes for Claude sessions

Local RAG over the REMIND model source (GAMS, R, docs, scenario configs), served as an MCP server to Claude Code
(and later opencode + Qwen). The question behind it: does code retrieval beat static context files
(`remind-context/AGENT.md`) plus agentic grep, and is it worth maintaining instead of a curated corpus like
magpie-agent's? Argument and numbers: `../TODO.md` appendix A.

## Where things are

| what | where |
|---|---|
| Open work (phased, numbered tasks) | `../TODO.md` |
| All measured numbers | `FINDINGS.md`; audit rounds in `eval/rounds/round-NN.md` |
| Big-picture overview, glossary | `../HANDOFF-260924-Introducing_REMIND_RAG.md` |
| Structure diagram, commands | `README.md` |
| Code | `src/remind_rag/`: `chunkers.py` (GAMS/R/MD splitting), `enrich.py` (headers), `index.py` (build + parser checks), `search.py` (hybrid ranking, default status), `usage.py` (roles, if-guards, switch-condition evaluation), `links.py` (statements for `get_links`), `server.py` (MCP tools) |
| Tests (no LLM, no network) | `tests/test_tool_output.py`: each test replays a round-02 failure on the tool output |
| Evaluations | `eval/retrieval_eval.py` (local, free), `eval/audit.py` (headless Sonnet + Opus grader, paid), `eval/audit/questions.yaml` + `rubric.md` |
| Workspace siblings | `../remind/` (REMIND checkout, with the user's `remind-context/`), `../gms`, `../goxygen`, `../magpie/` (+ `magpie-agent/`) |

## Commands

```powershell
$env:UV_NATIVE_TLS = 1                                   # PIK network intercepts TLS (uv, HF downloads)
uv run pytest tests                                      # tool-output checks against the newest index
uv run python eval/retrieval_eval.py data/remind-bge.db  # recall@5 / MRR, compare with eval/results/*.txt
uv run python -m remind_rag.index --root ..\remind       # full rebuild, ~13 min CPU; prints parser checks
uv run python -m remind_rag.index --root ..\remind --no-embed --db <copy>   # uses/FTS only, seconds
```

## Conventions

- **Keep measured states reproducible.** Each index state gets a version: build into `data/remind-bge-vN.db` (next
  free N; `--no-embed` rebuilds go into a copy), verify, then copy it to `data/remind-bge.db`, which the MCP
  registration pins. Note numbers in `FINDINGS.md` or `eval/results/`. v3 = v2 + fixed where-used table.
- **Parser changes:** the build must end with `parser checks: 0 warning(s)`, and `retrieval_eval.py` must not drop.
- **Tool-output changes:** add or adjust a test in `tests/test_tool_output.py` that shows the information is present.
- **Evaluation isolation:** answerer runs use `claude -p --setting-sources user` (no project CLAUDE.md, this file
  included) and read-only tools; `audit.py` does this. Don't put a CLAUDE.md into `piam-rag/` itself, because
  `remind/` sits below it and would inherit it.
- **Never interact with the REMIND repository**: no edits to tracked files in `../remind/`, no issues, PRs or
  pushes. Stale comments and doc defects found on the way go into `../STALE_COMMENTS.md` and stay local.
- Raw SQL against SQLite (FTS5 + sqlite-vec); no ORM (reasoning in `../TODO.md` appendix B).

## Working with the user

- They are new to RAG/ML: explain concepts in plain language and justify exploratory steps before asking.
- Answer questions and pause; don't push the next plan right away. Offer next steps in a sentence.
- Improve the system with free local checks first; propose paid audit rounds only once the question set is
  better. Order plans by dependency and value, not by API cost.
- During a paid run, post a short overview per finished question (`eval/audit.py show`).
