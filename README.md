# remind-rag

A local search index over the REMIND source (GAMS core and modules, `main.gms` switches, scenario configs, R
scripts, tutorials), served as an MCP server to Claude Code (and opencode). Claude asks it instead of grepping:

- **exact lookups** of symbols, switches, modules and scenarios, with where each symbol is declared, assigned, used in
  equations and read;
- **whether code runs in a default run** (✓ / ⚠ / ?), checked against the switch values in `main.gms`;
- **one-hop links** through the code (what a value is computed from, what it feeds into);
- **search** for conceptual questions, by keyword and by meaning.

Everything runs locally: one SQLite file per REMIND checkout, a small embedding model on the CPU, no service to run.

**Part 1, use it:** [Setup](#setup) · [Register](#register-with-claude-code) · [Keep it current](#keep-it-current) ·
[Reading the results](#reading-the-results)
**Part 2, how it works:** [Structure](#structure) · [Code](#code) · [Evaluation](#evaluation) ·
[Sweeps](#sweeps-over-remind) · [Development](#development)

---

# Part 1: Use it

## Setup

`rag/` is a standalone checkout; put it anywhere, typically next to the REMIND checkout it serves. Below, `<rag>`
and `<remind>` are those two folders. `uv sync` creates `<rag>/.venv`; `uv run --directory <rag>` always uses it, also
when another venv (e.g. a Jupyter one) is active. Indexes and the downloaded embedding model live in `<rag>/data/`.

Linux (e.g. the PIK cluster):

```bash
cd <rag>
uv sync                                                   # behind a TLS-intercepting proxy: export UV_SYSTEM_CERTS=1
OMP_NUM_THREADS=4 uv run python -m remind_rag.index --root <remind> --model bge   # first build ~15 min; the thread cap keeps a login node usable
```

Windows (PowerShell):

```powershell
cd <rag>
$env:UV_SYSTEM_CERTS = 1        # PIK TLS proxy: use the Windows certificate store
uv sync
uv run python -m remind_rag.index --root <remind> --model bge   # first build ~15 min on a laptop CPU
```

The build ends by printing the `claude mcp add` line with this machine's paths.

**One model for everyone (HPC).** The embedding model (bge-small, 64 MB) goes to `<rag>/data/models` on the first
build. On a cluster, keep one shared copy instead: the maintainer fills a group-readable folder once,

```bash
REMIND_RAG_MODELS=<shared dir> uv run python -m remind_rag.embeddings download   # prints the model's sha256
chmod -R g+rX <shared dir>
```

and every user puts `export REMIND_RAG_MODELS=<shared dir>` into `~/.bashrc`. A shared folder is only read, never
downloaded into, so builds and searches work on compute nodes without internet; a missing model stops with the
command above. The build records the model's sha256, and the server tells Claude when the model on this machine
differs from the one that built the index.

Build options:
- `--db data/<name>.db`: one index per REMIND checkout. Several checkouts can share one `<rag>`; give each its own
  file and pin it at registration (below).
- `--gms-export`: module interfaces, `@limitations`, `not_used.txt` reasons; needs R with `gms` and `goxygen` in
  `r/library` (`R CMD INSTALL -l r/library <gms clone> <goxygen clone>`). Without it the index has no module
  interfaces.
- `--model jina`: ~4x slower and worse retrieval here (FINDINGS §3).
- Only git-tracked files are indexed in a git checkout: your own notes in the folder stay out.

## Register with Claude Code

Once per REMIND checkout, from inside it (local scope = stored in `~/.claude.json`, nothing written into the repo):

```bash
cd <remind>
claude mcp add remind-rag --scope local -e REMIND_RAG_DB=<rag>/data/remind-bge.db -e REMIND_RAG_ROOT=<remind>   -- uv run --offline --directory <rag> python -m remind_rag.server
```

Use absolute paths. `REMIND_RAG_DB` is the index (default: `<rag>/data/remind-bge.db`). `REMIND_RAG_ROOT` is the
checkout the server answers for (default: the folder the index was built from); it is compared with the commit
stored in the index. With `REMIND_RAG_MODELS` set, the line the build prints passes it on. Check with
`claude mcp get remind-rag`, and with `/mcp` inside Claude Code.

**opencode (untested):** `opencode.json` in the REMIND checkout:

```json
{
  "mcp": {
    "remind-rag": {
      "type": "local",
      "command": ["uv", "run", "--offline", "--directory", "<rag>", "python", "-m", "remind_rag.server"],
      "environment": {"REMIND_RAG_DB": "<rag>/data/remind-bge.db", "REMIND_RAG_ROOT": "<remind>"},
      "enabled": true
    }
  }
}
```

## Keep it current

After a `git pull` in REMIND, rerun the build command. A build always redoes everything, but takes each embedding
from the previous index (the `--db` target, or `--reuse <other.db>`) when the exact text to embed is unchanged, so it
takes seconds to a minute instead of ~15 min. Shifted line numbers re-embed nothing; a changed symbol description
re-embeds every chunk that uses the symbol (e.g. 26 chunks for `pm_taxCO2eqSum`). `--no-reuse` forces a full build.
Then `/mcp` → reconnect in Claude Code.

- **The index knows its REMIND commit.** When the checkout the server answers for is at another commit, the server
  tells Claude ("line numbers and code may differ; if a result doesn't match the file, trust the file").
- **Versioned builds.** Build into a new file (`--db data/remind-bge-vN.db`) and add `--promote`: it copies the build
  to the pinned `data/remind-bge.db` only if the parser checks report 0 warnings and retrieval (recall@5, MRR) is not
  lower than the pinned index's. Older builds stay as `-vN` files.
- **Safe swaps.** The build writes `<db>.building` and renames it at the end, so the pinned file never holds a
  half-built index. On Windows the rename fails while a server holds the file open: stop it first.
- **Upgrading remind-rag itself.** A new version may need a newer index; the server then refuses to start and prints
  the rebuild command (with `--reuse`, so it takes seconds).
- Copying an index to another machine works (paths inside are relative to the REMIND root).

## Reading the results

| tool | use for |
|---|---|
| `search(query, k, module, realization, kind, phase, scenario)` | conceptual / free-text questions; 5 hits by default (at most 20), each with `» status` lines (see below) and at most 12 text lines plus a `path:L1–L2` pointer to the rest; `scenario` drops unselected realizations and down-ranks code compiled out by that scenario's switches |
| `get_symbol(name, role, module, offset)` | exact GAMS identifiers: declaration, description + unit, interface owner (`declared in`) and users, equation with its domain condition (`Generated for`). Use sites as a summary per role (declared / assigned / in equations / read): counts per file with their default status, every use that doesn't run by default listed, up to 4 default assignments outside bounds (where the value is computed). `role="assigned"` (optionally `module=`, `offset=`) lists the lines |
| `get_switch(name)` | `cm_*` / `c_*` switches and module selections: docs, default, allowed values, the GAMS and R lines that use it (count, per file with status, the first 10, default-active first) |
| `get_module(name, realization)` | description, the default realization with its **steered by** block (abort preconditions, switches used as compile-time branch / run-time test / overwrite / read with their defaults, what the selecting scenario configs set), every alternative in one line with alarming verdicts (`CAN NEVER BE MET`, `aborts under the defaults unless …`) and how many configs select it, interfaces as names. `realization="…"` gives one realization in full |
| `get_scenario(name)` | a scenario's description, settings, and what differs from the `main.gms` defaults |
| `get_links(name, max_links)` | one hop through the code: what a symbol is computed from, what is computed from it, which equations it shares with which variables; linked names carry their declaring module, links that don't run by default are marked and listed last (at most half of `max_links`, default 10; the rest counted per module). Statements in one file with the same status share an entry (`; :22 → x`). Call it again on a linked name for the next hop |

**Default status.** Results say whether code runs in a default run. The defaults are the switch values in `main.gms`
(`config/default.cfg` sets no GAMS switches). Examples from `search`:
- `» realization MOFEX: NOT DEFAULT (default: grades2poly; selected by $fossil)`
- `» compiled only if not "%c_tech_earlyreti_rate%" == "off": INACTIVE by default`
- `» switch tests in this code (with default values): cm_emiscen = 6  (default cm_emiscen = 9) → false`

In `get_symbol`, `get_switch` and `get_links`, every entry states its status on its first line: `✓ runs by default`,
`✓ default realization`, `⚠ not in a default run` or `? depends on settings`, with the reasons on the lines below, e.g.
`⚠ inside if (cm_emiscen eq 6) → inactive by default` or `⚠ module switched off by default (none)`. The positive ✓
is there on purpose: with only ⚠ marks, an unmarked entry was read as "unknown" (FINDINGS §10). For equations,
`Generated for` shows the domain and `$`-condition that decide whether GAMS generates the equation at all.

Conditions have three possible values: true, false, or "depends on non-default settings". Anything the evaluator
can't read (function calls, set membership, comparisons between parameters) counts as unknown, so "depends" is
common and not a warning.

**Roles.** `assigned` means the name is the left-hand side of `=` or `.fx/.lo/.up/.l =` (also behind a `$(…)`
condition, and the `=` may be on a following line), or is loaded with `execute_load` / `$load`; every other use is
`read`, including `=` as a comparison inside `if(…)` / `$(…)`. Names in `!!` end-of-line comments are not uses.

---

# Part 2: How it works

## Structure

Numbers from index v9 (REMIND `develop` at e50744c05, built 2026-10-14): 673 files, 3.2 MB of source.

```
 REMIND checkout                                   how it is read                       chunks
 ────────────────────────────────────────────────────────────────────────────────────────────────
 GAMS   core/, modules/, main.gms,           ─►  GAMS-aware (regex, line based):         3,346
        standalone/, tests/                       declarations · equations+doc comment
                                                  · main.gms switches · $ifthen blocks
                                                  + symbol table (3,019) + uses (18,467)
 config scenario_config*.csv                 ─►  one chunk per scenario row               577
        default.cfg                          ─►  generic R splitting                         6
        settings_config.csv, mappings, conopt    NOT indexed
 R      start.R, output.R, scripts/          ─►  generic: split at functions/sections     193
                                                  (no symbol table, no call graph)
 docs   tutorials/, *.md, CHANGELOG          ─►  split at Markdown headings               512
 ─      input/, output/, renv/, doc/,            NOT indexed (data, generated, vendored;
        remind-context/                          remind-context = the static-context baseline)

 optional: r/export_gms.R (gms::codeCheck, goxygen)  ─►  module interfaces (1,252 rows, 88 chunks),
                                                         @limitations (21), not_used.txt (1,712 rows)

 INDEX BUILD  python -m remind_rag.index
 ────────────────────────────────────────────────────────────────────────────────────────────────
  corpus.py      which files ─► chunkers.py  split into chunks, extract symbols/switches/uses
  scenarios.py   scenario rows ─┤
  gmsdata.py     gms export ────┤
                                ▼
  enrich.py      plain-English header per chunk (module, realization, description of used symbols)
                                │
         ┌──────────────────────┴──────────────────────┐
         ▼                                             ▼
  FTS5 keyword index (BM25)                 embeddings.py: bge-small on CPU
  identifiers stay whole (vm_cap)           header + first 1,000 chars → 384 numbers
         └──────────────────────┬──────────────────────┘
                                ▼
  data/remind-bge.db  — one SQLite file (store.py), 23 MB:
    chunks (text + header + path:line)   ~6.5 MB   what the tools return
    word index (no copy of the text)     ~1.8 MB   find by word
    vectors (4,743 × 384 floats)         ~7.9 MB   find by meaning
    symbol_uses, symbols, switches,      ~6.4 MB   exact lookups without ML
    scenarios, module_interfaces, not_used, preconditions (+ their SQLite indexes)

 QUERY TIME  python -m remind_rag.server   (started by Claude Code over stdio, no running service)
 ────────────────────────────────────────────────────────────────────────────────────────────────
  Claude Code / opencode ──MCP──► server.py
     search(query, filters)  ─► search.py: BM25 ranking ┐
                                           vector ranking ├─► RRF fusion ─► filters ─► top-k chunks
                                           named symbol / ┘   (module, realization,       with path:line
                                           scenario first      kind, phase, scenario)
     get_symbol · get_switch · get_module · get_scenario · get_links  ─► SQL on the tables above
```

**Hybrid search.** Keyword search (BM25) finds exact words and identifiers; vector search finds chunks by meaning
when the question's words don't occur in the code ("hydrogen" vs `H2`). They fail on different questions, so their
rankings are merged by reciprocal rank fusion (only ranks count, not scores). Hybrid finds 81 % of the expected chunks
in the top 5, keyword alone 71 % (FINDINGS §3, §13).

**How `get_links` finds links.** It needs nothing beyond the index: for each line where the symbol appears, it takes
the whole GAMS statement from the chunk text (from the previous `;` to the next), and the stored roles say which
symbol is on the left of the `=`. The symbol's own assignments give "computed from" (the other symbols from that line
on; conditions of an enclosing `if` are left out), statements that read it and assign something else give "feeds
into", and equation bodies give "in equations". Statements without an assignment (display, `if` conditions, output)
are only counted.

## Code

The package `src/remind_rag/` has four groups of modules: reading REMIND's sources, building the index, answering
(the MCP tools), and reports. The map with one line per module is the docstring of `src/remind_rag/__init__.py`.

| path | what |
|---|---|
| `src/remind_rag/` | the Python package |
| `r/export_gms.R`, `r/library/` | optional gms/goxygen export; project-local R library (not in git) |
| `data/` | index files (`remind-<model>[-vN].db`), downloaded embedding models, `gms_export.json` (not in git) |
| `eval/` | retrieval and answer evaluations, raw `results/`, graded `rounds/` |
| `tests/` | tests; `tests/reference/` = recorded tool output and tool descriptions |
| `FINDINGS.md` | all measured numbers, per version |

Development files (`CLAUDE.md`, `.claude/skills/`, eval and sweep commands) assume the author's workspace: REMIND at
`../remind`, notes in `../TODO.md` and `../STALE_COMMENTS.md`. Using the server needs none of them.

## Evaluation

```powershell
uv run python eval/retrieval_eval.py data/remind-bge.db   # recall@5 / MRR per mode (bm25, vector, hybrid), local and free
uv run python eval/e2e_eval.py --remind ..\remind --model sonnet   # headless Claude, baseline vs RAG
```

Questions with verified reference answers: `eval/questions.yaml`. Results: `FINDINGS.md`.

**Audit rounds (answer quality).** `eval/audit.py` asks the 20 audit questions (`eval/audit/questions.yaml`, with
answer keys) to headless Sonnet in the REMIND checkout and records each answer with the tools it actually called. A
headless Opus grader then grades it against the source (read-only, no RAG, blind to the setup) per
`eval/audit/rubric.md`. These runs cost money.

```powershell
uv run python eval/audit.py run    --round 2 --grade            # arms context+rag and context, 2 questions in parallel
uv run python eval/audit.py show   --round 2 Q5                 # one question, both arms side by side
uv run python eval/audit.py report --round 2                    # → eval/rounds/round-02.md
```

Isolation: `--setting-sources user` (no project/parent `CLAUDE.md`), built-in tools limited to Read/Grep/Glob,
`--strict-mcp-config`; remind-context is injected via `--append-system-prompt-file`. Existing answers/grades are
kept (so a run can be resumed); `--force` redoes them. To disagree with a grade, put the changed fields into
`eval/rounds/round-NN/grades/<Q>-<arm>.override.json`; `report` marks them ✎.

## Sweeps over REMIND

Two mechanical sweeps, no LLM, seconds per run, read-only on the checkout. They complement `gms::codeCheck`, which
checks code structure and strips all comments first; these check the comments and abort conditions against the code.

```powershell
uv run python -m remind_rag.preconditions --root ..\remind [--all]   # abort preconditions: impossible / aborts by default
uv run python -m remind_rag.doccheck --root ..\remind [--module modules/45_carbonprice] [--md out.md]
uv run python -m remind_rag.hierarchy [--json out.json]             # module coupling, from the index
```

- `preconditions.py`: every `abort` in `modules/` and `core/`, with the switch settings that trigger it; flags
  preconditions that name a nonexistent switch, realization or value, realizations that abort under the defaults, and
  abort messages that name another realization.
- `doccheck.py`: prose (comments, `!!`, declaration descriptions) against facts from the code: names that occur in no
  code, stated defaults vs `main.gms`, file pointers to missing files or files that don't mention the subject,
  nonexistent realization names. Regex extraction plus lookups; semantic mismatches need a reader.
- `hierarchy.py`: which module declares, writes and uses each interface symbol, in a default run and in all code.

`tests/test_sweeps.py` pins known finds and tuned-away noise. Reports and the verified list live outside this repo.

## Development

```powershell
uv run pytest tests                       # needs data/remind-bge.db (or REMIND_RAG_DB) and the REMIND checkout
uv run ruff check; uv run ruff format     # lint and format (line length 120)
uv run python tests/test_reference_output.py --update   # only for deliberate output changes, in the same commit
```

- `tests/test_reference_output.py` compares 15 tool calls and the tool descriptions byte for byte with
  `tests/reference/`: the safety net for refactoring.
- Examples in docstrings (`>>>`) run as tests (`tests/test_doctests.py`).
- `git config blame.ignoreRevsFile .git-blame-ignore-revs` hides the one formatting commit from `git blame`.
