# REMIND RAG — findings (2026-09-24)

*Version v0.0.1 — indexes kept as `data/remind-bge-v0.db` / `data/remind-jina-v0.db`, raw eval runs in
`eval/results/`.*

Minimal local RAG over REMIND code + docs (674 files, 3.2 MB → 3,500 chunks), exposed to Claude Code as an
MCP server (`search`, `get_symbol`, `get_switch`, `list_realizations`). Question: does it beat "a bunch of
context files", how does it scale, how does it integrate?

**Short answer:** yes for *code* questions, as a complement to (not a replacement for) the static context.
Same or slightly better answer quality, ~40 % fewer turns and time, ~10–15 % lower cost, and it fixed the one
factual error that all non-RAG arms made. The static `AGENT.md` context is a different kind of knowledge
(procedures) and belongs next to it — but see the *AGENT.md is not loaded* finding first.

## 1. `AGENT.md` is not loaded by Claude Code (fix this first)

Claude Code reads `CLAUDE.md`, not `AGENT.md`. In this checkout, a headless session with tools disabled reports
no project instructions at all; with a `CLAUDE.md` containing `@remind-context/AGENT.md` it quotes the
R 4.3.2 rule correctly (tested in a scratch copy, not in the repo). Absolute-path imports outside the
project are *not* loaded headlessly (they need a one-time approval).

→ `remind-context/bootstrap.{ps1,sh}` should also write `CLAUDE.md` → `@remind-context/AGENT.md`
(and the README claim "Consumed by Claude Code" is currently only true with that pointer).

## 2. End-to-end: headless Claude Code (Sonnet), 10 questions × 4 arms

Arms: **baseline** (Read/Grep/Glob only), **rag** (+ MCP server), **context** (+ `remind-context/AGENT.md`
with imports expanded, injected as a CLAUDE.md would be), **context+rag**. Index: bge-small. Graded 0–2
against hand-verified reference answers (`eval/questions.yaml`).

| arm | score | mean cost | mean turns | mean time | mean input tokens |
|---|---|---|---|---|---|
| baseline | 18/20 | $0.080 | 5.9 | 25 s | 103k |
| rag | 19/20 | $0.070 | 3.7 | 16 s | 88k |
| context | 18/20 | $0.085 | 5.8 | 25 s | 125k |
| context+rag | 19/20 | $0.073 | 3.4 | 15 s | 96k |

What stands out:
- **Speed, not quality, is the main win** on these questions: Claude + grep is already good at finding things
  in 3 MB of code. RAG saves turns most on questions that span files (early retirement: 11 → 3–7 turns;
  CDR capture into core: 7–11 → 4–5; restart docs: 7 → 2).
- **The one systematic error** (DAC energy demand): both non-RAG arms read `p33_fedem = 5.28` from
  `datainput.gms` and reported "5.28 GJ/tCO2" — it is EJ/GtC = GJ/**tC** (1.44 GJ/tCO2). Both RAG arms got the
  right per-CO2 numbers, because `search` surfaced the realization's prose description first.
- **RAG does not fix interpretation:** both RAG arms mis-stated the weathering-rate multipliers (0.94/0.29)
  as yearly fractions even though the retrieved chunk contained the full definition.
- **Static context is neutral on code questions** (+22k input tokens per session, no quality change) —
  expected, since `AGENT.md` holds procedures (debugging protocol, renv, HPC), not code facts. It should be
  judged on debugging/workflow tasks, not on this question set.

Caveats: n=10 questions, one run per arm (LLM runs vary), grading by me against my own reference answers.

## 3. Retrieval quality (21 questions, recall@5 / MRR@10)

| index | bm25 | vector | hybrid |
|---|---|---|---|
| bge-small (384d) | 76 % / 0.65 | 76 % / 0.64 | **90 % / 0.74** |
| jina-v2-base-code (768d) | 76 % / 0.65 | 48 % / 0.43 | 67 % / 0.52 |

- **The code-specialised model lost clearly** (vector-only 48 % vs 76 %), while being ~4× slower to build and
  to query. Checked for bugs (stored vectors = fresh ones; sensible on toy examples) — it is genuinely worse
  here. Likely reason: after enrichment most of the embedded text is English (headers, descriptions, doc
  comments), which bge-small is trained to retrieve, and GAMS is not among the languages jina-code was
  trained on. Lesson: "code model" ≠ better for GAMS; without the eval we would have picked the wrong one.
  bge-small is the default.
- Keyword and vector search fail on *different* questions (keyword: plain-English questions whose words don't
  occur in code, e.g. "hydrogen" vs `H2`/`pebiolc`; vector: exact identifiers buried in many chunks), which is
  why hybrid wins.
- Two query-time heuristics carry the symbol questions (60 % → 100 %): if the query contains a known
  identifier (anything with `_`, e.g. `vm_cap`), its declaration/definition chunks are ranked first; plain-
  language queries down-weight BM25 (0.5). A first version that also treated plain words as identifiers
  misfired ("biomass", "tax" are module-selection switch names).
- These were tuned on the same 21 questions → treat 90 % as optimistic. One further tweak (BM25 0.25) made
  things worse and was reverted.
- Remaining misses are cross-module questions phrased in plain language. `get_symbol` (with the enclosing
  equation for every use site) covers "where is X used" better than `search` does.

## 4. Scalability

| | bge-small | jina-v2-base-code |
|---|---|---|
| embedding time (i7-1365U laptop, CPU) | 9.3 min | 35.5 min |
| index size | 18 MB | 28 MB |
| query latency (incl. query embedding) | ~15–20 ms | ~60–70 ms |
| chunking + symbol/uses extraction | 0.3 s | 0.3 s |

- **Embedding is the only expensive step**, and GAMS makes it worse: ~2 characters per token (identifiers split
  into many sub-word tokens), so a typical 600–1,200-char chunk hits bge's 512-token limit. Mitigations in
  place: embed only header + first 1,000 chars (BM25 still sees everything), sort by length before batching.
- **Querying scales trivially**: sqlite-vec brute force over 3.5k vectors is milliseconds; 50–100k chunks would
  still be well under a second. Everything is one SQLite file, no server.
- **Phase 2 (pik-piam packages)**: the ~25 packages are mostly R, which tokenizes better than GAMS. A rough
  estimate (unmeasured) is 3–5× the current corpus → ~30–45 min with bge on this laptop, a few hours with jina.
  Worth adding before that: incremental re-indexing (hash per file, re-embed only changed files), and/or
  building the index once on the cluster and distributing the `.db` file.

## 5. Integration

- **Claude Code:** works as a stdio MCP server; `claude mcp add remind-rag --scope local -- uv run --offline
  --directory <rag> python -m remind_rag.server` (nothing written into the REMIND repo). Server start ≈ model
  load (a few seconds), then ~20 ms per query.
- **opencode + Qwen:** not tested yet. Same MCP server via `opencode.json` (see README). Expectation: smaller
  models benefit *more* from the dedicated tools (`get_symbol`, `get_switch`) and from the identifier-aware
  `search`, because they are weaker at multi-step grep strategies — this is the next thing to measure, with the
  same `eval/questions.yaml`.

## 6. Recommended next steps

1. Fix the `CLAUDE.md` pointer in `remind-context/bootstrap` (independent of RAG).
2. Use the MCP server day-to-day for a week; add questions it gets wrong to `eval/questions.yaml` (grow it to
   ~50, including debugging/workflow questions where `AGENT.md` should matter).
3. Test opencode + Qwen against the same server and eval set.
4. Phase 2: pik-piam packages, with incremental indexing.
