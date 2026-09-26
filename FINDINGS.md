# REMIND RAG — findings (2026-09-24)

Sections 1–6 describe **v0.0.1** (indexes kept as `data/remind-bge-v0.db` / `data/remind-jina-v0.db`);
section 7 describes **v0.1.0** (gms/goxygen, switch conditions, scenarios); section 8 describes audit round 02
and **v0.2.0** (default status, roles, parser checks); section 9 describes **v0.3.0** (`get_links`, index v3); section 10 **v0.4.0** (index v4: role fixes, `get_module` "steered by", ✓/⚠/? status marks). Raw eval runs are in `eval/results/`, audit rounds in
`eval/rounds/`.

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

## 7. v0.1.0 — gms/goxygen, switch conditions, scenarios

### What changed
- **`gms::codeCheck` (0.35.0) + `goxygen::extractDocumentation` (1.5.1)**, installed from the local clones
  (identical to pik-piam upstream) into `r/library`, exported by `r/export_gms.R` (2.6 s) and ingested when
  `data/gms_export.json` exists — the index still builds without R.
  - per module an *Outputs* and an *Inputs* chunk ("provides vm_x → used by core, 47_regipol …");
  - `get_symbol` shows provider/consumers and `not_used.txt` reasons; new `get_module` (description,
    realizations + default, limitations, interfaces);
  - 21 `@limitations` blocks as typed chunks.
- **`$ifthen` conditions** (from the Gemini blueprint): 416 `$ifthen` blocks tracked per line; chunks never
  span a condition change; 530 chunks carry "Only compiled if: …" in header + metadata.
- **Scenarios**: 577 rows of `config/scenario_config*.csv` as chunks (with `copyConfigFrom`), `get_scenario`
  (settings + diff to `main.gms`), and opt-in `search(scenario=…)`: drops realizations the scenario does not
  select, down-ranks code compiled out by its switch values. `run=<output folder>` (via `cfg.txt`) is not
  implemented — no run folders here to test against.
- Query side: a query naming an identifier also pulls that symbol's interface record; a query naming a
  scenario pulls its row; identical declarations (one per realization) count once. Module names in headers
  are spelled out ("carbonRemoval (carbon removal)").

### Side results
- **Our regex parser was already complete**: codeCheck finds 1,858 declarations, our parser misses none
  (it additionally sees 41 `c_*` switches in `standalone/`). codeCheck's value is the *interfaces*.
- gms 0.35 runs codeCheck in ~2 s (token lexer); the installed 0.33.6 was much slower — worth upgrading
  the system library too.
- Note: codeCheck's "provided by" is the *declaring* module (e.g. `pm_taxCO2eqSum` "from 46_carbonpriceRegi",
  although `core/presolve.gms:10` computes it); many `not_used.txt` reasons are placeholders ("???",
  "questionnaire").
- Two doc bugs in REMIND found on the way: `modules/11_aerosols/exoGAINS2025/realization.gms:18` has the
  `@limitations` text of EDGE-transport (copy-paste); scenario `SSP2-PkBudg1000` describes itself as
  "SSP2-PkBudg1050 … 1150 Gt" (`config/scenario_config.csv:12`).

### Retrieval (29 questions = 21 old + 8 new: interface, limitations, scenario; recall@5 / MRR@10, hybrid)

| index | ALL | interface | scenario | cross-module | symbol |
|---|---|---|---|---|---|
| bge v0 | 83 % / 0.65 | 50 % / 0.19 | 50 % / 0.25 | 67 % / 0.22 | 100 % / 0.90 |
| bge v1 draft (kept as `-v1-draft.db`) | 83 % / 0.70* | 50 % / 0.22 | 100 % / 1.00 | 67 % / 0.44 | 80 % / 0.70 |
| **bge v1** | **86 % / 0.74** | 75 % / 0.54 | 100 % / 1.00 | 33 % / 0.08 | 100 % / 0.90 |
| jina v0 | 62 % / 0.47 | 50 % / 0.17 | 0 % / 0.00 | 0 % / 0.06 | 80 % / 0.54 |
| jina v1 | 72 % / 0.63 | 100 % / 0.75 | 100 % / 1.00 | 0 % / 0.05 | 60 % / 0.60 |

jina gains most from v1 (62 → 72 %, MRR 0.47 → 0.63; best on interface questions) — the extra English
(interface chunks, spelled-out module names) suits it — but still trails bge clearly (vector-only 48 % vs
69 %) while costing 45 min to build (bge 11 min), 35.5 MB (bge 27.6 MB) and 2–3× query latency. bge stays
the default; both v1 indexes are kept.

(*79 % / 0.67 before the query-side scenario detection was added.) The first v1 draft *regressed*: long
"interfaces declared here" header lines pushed chunk text out of the 1,000-char embedding window, and the
combined in/out interface list was split arbitrarily. Fixed by compact headers and separate in/out chunks.
Per-category numbers move in steps of 25–100 % (1–4 questions each) and the set was used for tuning, so only
the overall trend is meaningful; I stopped tuning at this point.

### End-to-end (headless Sonnet, Read/Grep/Glob ± MCP, graded 0–2)

| 8 new questions | score | mean cost | mean turns | mean time | input tokens |
|---|---|---|---|---|---|
| baseline | 15/16 | $0.129 | 5.8 | 26 s | 139k |
| rag v1 | 16/16 | $0.092 | 3.4 | 12 s | 96k |

- Largest gains on **scenario** questions: baseline 10–13 turns / $0.21–0.24 each (grepping through CSVs
  and `main.gms`), RAG 4–5 turns / ~$0.11 via `get_scenario` / scenario chunks.
- Baseline's one error: "what does module 33 need" — incomplete list and an output (`vm_co2capture_cdr`)
  listed as input; RAG answered from the codeCheck interfaces, complete.
- Rerun of 3 old cross-module questions with v1: 5/6 (v0: 6/6) — one answer cited the wrong core equation
  despite the tools returning the right line. Single runs; within LLM variance.

## 8. Audit round 02 (2026-09-25) and v0.2.0

### Round 02: 20 questions × 2 arms, blind Opus grader
Headless Sonnet with `remind-context` as static context, with and without the MCP server (v0.1.0 index). The Opus
grader checks each claim against the code and doesn't know which arm it is grading. Rubric: `eval/audit/rubric.md`.
Full report: `eval/rounds/round-02.md`.

| arm | Critical | Major | Minor | traps caught | mean cost | mean turns | mean time |
|---|---|---|---|---|---|---|---|
| context+rag | 1 | 14 | 33 | 4/5 | $0.09 | 5.5 | 21 s |
| context | 1 | 14 | 39 | 4/5 | $0.12 | 10.0 | 40 s |

- **Same correctness, about twice as fast, 25 % cheaper.** The RAG doesn't make answers more correct yet.
- **Largest failure class: "default not checked"** (10 of 28 classified failures, both arms). Answers describe code
  from non-default realizations or `$ifthen` / `if(…)` branches that a default run never executes. The v0.1.0 tool
  output contained no information about which code is active by default.
- **Attribution** (Q10, Q11): the declaring module (codeCheck's interface owner) was presented as the module that
  computes the value.
- **Equation mechanics** (Q18): the `$`-condition in an equation's domain decides whether GAMS generates it, and the
  output didn't show it. The parser missed 64 of 298 equations whose head spans several lines (#25).
- Grader calibration against the hand grades of round 01: Q1 matched exactly, Q18 came out one step stricter.
- The user's verdict: several questions and answer keys need work (TODO #16) before the next paid round.

### v0.2.0: changes to the tool output
- **Default status**: `» …` lines on search hits, ⚠ tags on `get_symbol` use lines, `[DEFAULT]` / `[not default]`
  in `get_module`, with realization, `$ifthen` conditions and run-time `if(…)` / `$(…)` switch tests, each evaluated
  against the `main.gms` defaults as true, false or "depends" (`usage.py` `eval_expr`). A module whose default is
  `off` / `none` is called "switched off by default".
- **Roles** in `symbol_uses`: declared / assigned / used in equations / read; `get_symbol` groups by role.
- **Parser**: multi-line equation heads (298/298 equations found), equation chunks up to 12k chars (`qm_budget` in
  one piece). Build-time checks (every declared equation has a definition, `$ifthen`/`$endif` balanced, each module
  has a module doc) print a warning list and store it in the index meta: currently 0 warnings; 0 codeCheck
  declarations missed.
- **Tests**: `tests/test_tool_output.py`, 16 checks that replay round-02 failures on the tool output (no LLM).
- **Retrieval unchanged**: hybrid 81 % recall@5 / MRR 0.69, same as before (`eval/results/retrieval-v2-defaults.txt`).
  The status information only goes into the tool output, not into the embedded text.
- Index: 4,755 chunks, 18,769 symbol uses (`data/remind-bge.db` = `remind-bge-v2.db`).

### Not measured yet
Whether answers actually get more correct. That needs round 03, which waits for a better question set.
Known limits of the markers: the evaluator treats anything it can't read as unknown ("depends"). Roles come from
single lines, so assignments inside loops or macros that span lines can come out as `read` (partly fixed in v3,
section 9).

## 9. v0.3.0: `get_links` and index v3 (2026-09-25)

### Smoke test in Claude Code (v0.2.x)
Three interactive questions in a REMIND session all used the MCP tools and led with the default run. Q1
(pm_taxCO2eqSum) was correct, including the attribution (46 declares it, core computes it). That was a Major error in
round 02. Q2 found that NPi2025's description contradicts its code. Q3 missed that temperatureNotToExceed can't
compile, because single-line `$ifi … abort` preconditions aren't shown (TODO #32). Side find: until then the MCP
registration had pointed at the v0 index. It now pins `data/remind-bge.db`.

### `get_links`
One hop through the code, computed at query time from chunks + `symbol_uses` (no new tables, no re-embedding):
*computed from* / *feeds into* / *in equations*, each link with path:line, the declaring module of linked symbols
where it differs, and ⚠ default status (inactive links sorted last). Example: `pm_taxCO2eqSum` → computed in
`core/presolve.gms:10` from `pm_taxCO2eq`, `pm_taxCO2eqRegi [46]`, `pm_taxCO2eqSCC [51]` (3 of 13 assignments run
by default); feeds `p_priceCO2` and the 21_tax revenue parameters; shares equations with `vm_co2eq`,
`vm_emiMacSector`, `vm_emiAllco2neg [21_tax]` … Not measured with an LLM yet (targets Q13/Q18-type chains).

### Index v3 = v2 + fixed where-used table (`--no-embed`, seconds)
- Names in `!!` end-of-line comments were counted as uses: 245 false uses removed (18,769 → 18,524).
- Assignments whose `=` is on a following line (`x(t)$(…)` / `vm_x.fx(…)` on one line, `=` below) were `read`:
  +244 assignments now recognized (4,388 → 4,632). Sampled by hand; the `if(`/`loop(` opening lines and `$ifthen`
  lines stay `read` (the lines after them only supply the `=` for this line's own occurrence).
- 0 uses changed from assigned to something else. Retrieval identical to v2 in every mode and category
  (`eval/results/retrieval-v3-uses.txt`). Tests: 26 pass.

## 10. v0.4.0: index v4, role fixes, "steered by" in `get_module` (2026-09-26)

### Index v4 = v3 + two role fixes + `preconditions` table (`--no-embed`, seconds)
- **Comparisons counted as assignments.** `role()` accepted a name after `(` as the start of a statement, so
  `if(cm_nucscen = 5,`, `elseif(…)`, `$(pm_data(…) = 4)` and `break$(… = 1)` were `assigned`. Only `for (x = …`
  needs the `(`. 73 uses assigned → read, 51 of them switches.
- **Conditional assignments with a space after `$` counted as reads.** `vm_cap.lo(t,regi,te,"1") $ (t.val >= 2030)
  = 1e-7;` wasn't recognized (only `$(` was), which is REMIND's usual style for bounds. 143 uses read → assigned,
  almost all bounds (`vm_cap`, `vm_deltaCap`, `vm_capEarlyReti`, `vm_co2CCS`) and calibration values
  (`pm_cesdata_sigma`). Example: `get_symbol("vm_deltaCap")` assigned 80 → 108 lines, read 58 → 30.
- Bug 1 partly hid bug 2: `p_capCum(…) $ (p_capCum(…) = 0) = …` counted as assigned only via the comparison.
- Totals: assigned 4,632 → 4,702, read 8,854 → 8,784. Retrieval identical to v3 in every mode and category
  (`eval/results/retrieval-v4-roles.txt`). Roles only reach tool output (`get_symbol`, `get_links`), not embeddings.
- New table `preconditions` (100 aborts from `preconditions.py`, built at index time, also by `--no-embed`).

### `get_module`: "Steered by" per realization
From existing tables, no new parsing: abort preconditions (those that fail under the defaults or can never be met
in full, the rest folded into one line), switches the realization's code uses, split into compile-time branches
(`$ifthen`, from chunk conditions), run-time tests, overwrites and reads, each with its `main.gms` default;
switches shared by all realizations listed once; and "in practice": how many scenario configs select the
realization and what they set alongside. Example: all 20 configs that select `46/netZero` set
`cm_multigasscen = 2`, the value its abort requires (default 3). `get_module("45")`: 21.7k → 27.5k characters.
Tests: 66 pass (11 role cases, 5 `get_module` checks).

### ✓ / ⚠ / ? status on every entry (`get_symbol`, `get_links`)
Smoke test: asked how the carbon price affects the economy, the agent called `q33_carbonRemovalspending`
(33/portfolio, the default) a non-default alternative. The tool output was right ("6 equations, 6 active by
default", no ⚠ on the entry), but a missing ⚠ was the only sign. Every entry now carries its status on its first
line, and "depends on settings" is counted separately instead of as inactive. A rerun of the question got the
defaults right (n = 1).
