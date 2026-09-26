# remind-rag

Minimal local RAG over the REMIND source (GAMS core/modules, `main.gms` switches, R scripts, tutorials),
exposed as an MCP server for Claude Code (and opencode).

- Structure-aware chunking of GAMS: declaration blocks, equations (with their `*'` doc comment),
  `main.gms` switches (docs + default + allowed values), module/realization descriptions.
- Symbol table from declarations + where-used index over all GAMS/R lines (with enclosing equation).
- `$ifthen` switch conditions per chunk; scenario rows from `config/scenario_config*.csv`.
- Optional: module interfaces (who provides / consumes what), `@limitations` docs and `not_used.txt` reasons
  from PIK's `gms::codeCheck` and `goxygen::extractDocumentation` (`r/export_gms.R`).
- Hybrid retrieval: SQLite FTS5 (BM25, identifiers kept whole) + sqlite-vec (local CPU embeddings via
  fastembed), merged with reciprocal rank fusion. Everything lives in one SQLite file.

## Structure

Numbers are from the current index (`data/remind-bge.db`, built 2026-09-24: 674 files, 3.2 MB of source).

```
 REMIND checkout (../remind)                       how it is read                       chunks
 ────────────────────────────────────────────────────────────────────────────────────────────────
 GAMS   core/, modules/, main.gms,           ─►  GAMS-aware (regex, line based):         3,308
        standalone/, tests/                       declarations · equations+doc comment
                                                  · main.gms switches · $ifthen blocks
                                                  + symbol table (3,019) + uses (20,546)
 config scenario_config*.csv                 ─►  one chunk per scenario row               577
        default.cfg                          ─►  generic R splitting                         6
        settings_config.csv, mappings, conopt    NOT indexed
 R      start.R, output.R, scripts/          ─►  generic: split at functions/sections     193
                                                  (no symbol table, no call graph)
 docs   tutorials/, *.md, CHANGELOG          ─►  split at Markdown headings               513
 ─      input/, output/, renv/, doc/,            NOT indexed (data, generated, vendored;
        remind-context/                          remind-context = the static-context baseline)

 optional: r/export_gms.R (gms::codeCheck, goxygen)  ─►  module interfaces (1,252 rows, 88 chunks),
                                                         @limitations (21), not_used.txt (1,712 rows)

 INDEX BUILD  python -m remind_rag.index   (src/remind_rag/)
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
  data/remind-bge.db  — one SQLite file (store.py), 27.6 MB:
    chunks (text + header + path:line)   ~4.6 MB   what the tools return
    FTS5 copy + word index                ~5.8 MB   find by word
    vectors (4,706 × 384 floats)          ~7.9 MB   find by meaning
    symbols, symbol_uses, switches,       ~1–2 MB   exact lookups without ML
    scenarios, module_interfaces, not_used, preconditions
    + SQLite indexes / page overhead

 QUERY TIME  python -m remind_rag.server   (started by Claude Code over stdio, no running service)
 ────────────────────────────────────────────────────────────────────────────────────────────────
  Claude Code / opencode ──MCP──► server.py
     search(query, filters)  ─► search.py: BM25 ranking ┐
                                           vector ranking ├─► RRF fusion ─► filters ─► top-k chunks
                                           named symbol / ┘   (module, realization,       with path:line
                                           scenario first      kind, phase, scenario)
     get_symbol · get_switch · get_module · get_scenario  ─► plain SQL on the tables above
```

Folder layout:

| path | what |
|---|---|
| `src/remind_rag/` | the Python package: indexer, search, MCP server |
| `r/export_gms.R`, `r/library/` | optional gms/goxygen export; project-local R library (not in git) |
| `data/` | index files (`remind-<model>[-vN].db`), build logs, downloaded embedding models, `gms_export.json` |
| `eval/` | `questions.yaml`, `retrieval_eval.py`, `e2e_eval.py`, raw `results/`, `rounds/` (graded rounds), `cases/` (logged failures) |
| `FINDINGS.md` | all measured numbers |

## Setup

```powershell
cd rag
$env:UV_NATIVE_TLS = 1          # PIK TLS proxy: use the Windows certificate store
uv sync

# optional, needs R: current gms/goxygen from the sibling clones into a project-local library
R CMD INSTALL -l r/library ..\gms ..\goxygen

uv run python -m remind_rag.index --root ..\remind --model bge --gms-export   # ~15 min on a laptop CPU
uv run python -m remind_rag.index --root ..\remind --model jina   # optional: ~4x slower AND worse retrieval here (see FINDINGS)
```

Indexes land in `data/remind-<model>.db`; the server uses `REMIND_RAG_DB` or, by default,
the newest complete `remind-bge*.db`, else `remind-jina*.db`. Superseded indexes are kept with a
`-vN` suffix (e.g. `remind-bge-v0.db` = the v0.0.1 index).

## Use from Claude Code

Register once per REMIND checkout (local scope = stored in `~/.claude.json`, nothing written into the repo):

```powershell
cd ..\remind
claude mcp add remind-rag --scope local -e REMIND_RAG_DB=C:/Users/tonnru/lab/piam-rag/rag/data/remind-bge.db -- uv run --offline --directory C:\Users\tonnru\lab\piam-rag\rag python -m remind_rag.server
```

`REMIND_RAG_DB` pins the stable name `remind-bge.db`, which always holds the current index; older builds are kept as
`remind-bge-vN.db`. Without it, the server takes the newest complete `remind-bge*.db` by modification time, which can
be a draft build. Check with `claude mcp get remind-rag`, and with `/mcp` inside Claude Code (reconnect there after
a rebuild). Tools:

| tool | use for |
|---|---|
| `search(query, k, module, realization, kind, phase, scenario)` | conceptual / free-text questions; every hit carries `» status` lines (see below); `scenario` drops unselected realizations and down-ranks code compiled out by that scenario's switches |
| `get_symbol(name)` | exact GAMS identifiers: declaration, description + unit, interface owner (`declared in`) and users, equation with its domain condition (`Generated for`), use sites grouped by role: declared / assigned / in equations / read |
| `get_switch(name)` | `cm_*` / `c_*` switches and module selections: docs, default, allowed values, references |
| `get_module(module)` | description, realizations marked `[DEFAULT]` / `[not default]`, limitations, **steered by** per realization (abort preconditions, switches used as compile-time branch / run-time test / overwrite / read with their defaults, what the scenario configs that select it set), interfaces (inputs/outputs) |
| `get_scenario(name)` | a scenario's settings and what differs from the `main.gms` defaults |
| `get_links(name, max_links)` | one hop through the code: what a symbol is computed from, what is computed from it, which equations it shares with which variables; linked names carry their declaring module, links that don't run by default are marked and listed last. Call it again on a linked name for the next hop |

**Default status.** Results say whether code runs in a default run. The defaults are the switch values in `main.gms`.
Examples:
- `» realization MOFEX: NOT DEFAULT (default: grades2poly; selected by $fossil)`
- `» compiled only if not "%c_tech_earlyreti_rate%" == "off": INACTIVE by default`
- `» switch tests in this code (with default values): cm_emiscen = 6  (default cm_emiscen = 9) → false`

In `get_symbol`, use lines get the same information as ⚠ tags, e.g. `⚠ inside if (cm_emiscen eq 6) → inactive by
default` or `⚠ module switched off by default (none)`. For equations, `Generated for` shows the domain and
`$`-condition that decide whether GAMS generates the equation at all. Conditions have three possible values: true, false,
or "depends on non-default settings". Anything the evaluator can't read (function calls, set membership, comparisons
between parameters) counts as unknown, so "depends" is common and not a warning. Roles: `assigned` means the name is
the left-hand side of `=` or `.fx/.lo/.up/.l =` (also behind a `$(…)` / `$ (…)` condition, and the `=` may be on a
following line), or is loaded with `Execute_load`/`$load`; every other use is `read`, including `=` as a comparison
inside `if(…)` / `$(…)`. Names in `!!` end-of-line comments are not uses.

**How `get_links` finds links.** It needs nothing beyond the index: for each line where the symbol appears, it takes
the whole GAMS statement from the chunk text (from the previous `;` to the next), and the stored roles say which
symbol is on the left of the `=`. The symbol's own assignments give "computed from" (the other symbols from that line
on; conditions of an enclosing `if` are left out), statements that read it and assign something else give "feeds
into", and equation bodies give "in equations". Statements without an assignment (display, `if` conditions, output)
are only counted.

## Use from opencode (untested)

`opencode.json` in the REMIND checkout:

```json
{
  "mcp": {
    "remind-rag": {
      "type": "local",
      "command": ["uv", "run", "--offline", "--directory", "C:/Users/tonnru/lab/piam-rag/rag", "python", "-m", "remind_rag.server"],
      "enabled": true
    }
  }
}
```

## Evaluation

```powershell
uv run python eval/retrieval_eval.py data/remind-bge-v0.db data/remind-jina-v0.db   # recall@5 / MRR per mode
uv run python eval/e2e_eval.py --remind ..\remind --model sonnet                # headless Claude, baseline vs RAG
```

Questions + verified reference answers: `eval/questions.yaml`. Findings: `FINDINGS.md`.

### Audit rounds (answer quality)

`eval/audit.py` asks the 20 audit questions (`eval/audit/questions.yaml`, with answer keys) to headless Sonnet in the
REMIND checkout and records each answer with the tools it actually called. A headless Opus grader then grades it
against the source (read-only, no RAG, blind to the setup) per `eval/audit/rubric.md`.

```powershell
uv run python eval/audit.py run    --round 2 --grade            # arms context+rag and context, 2 questions in parallel
uv run python eval/audit.py show   --round 2 Q5                 # one question, both arms side by side
uv run python eval/audit.py report --round 2                    # → eval/rounds/round-02.md
```

Isolation: `--setting-sources user` (no project/parent `CLAUDE.md`), built-in tools limited to Read/Grep/Glob,
`--strict-mcp-config`; remind-context is injected via `--append-system-prompt-file`. Existing answers/grades are
kept (so a run can be resumed); `--force` redoes them. To disagree with a grade, put the changed fields into
`eval/rounds/round-NN/grades/<Q>-<arm>.override.json`; `report` marks them ✎.

## Sweeps over REMIND (stale docs and checks)

Two mechanical sweeps, no LLM, seconds per run, read-only on the checkout. They complement `gms::codeCheck`, which
checks code structure and strips all comments first; these check the comments and abort conditions against the code.

```powershell
uv run python -m remind_rag.preconditions --root ..\remind [--all]   # abort preconditions: impossible / aborts by default
uv run python -m remind_rag.doccheck --root ..\remind [--module modules/45_carbonprice] [--md out.md]
```

- `preconditions.py`: every `abort` in `modules/` and `core/`, with the switch settings that trigger it; flags
  preconditions that name a nonexistent switch, realization or value, realizations that abort under the defaults, and
  abort messages that name another realization.
- `doccheck.py`: prose (comments, `!!`, declaration descriptions) against facts from the code: names that occur in no
  code, stated defaults vs `main.gms`, file pointers to missing files or files that don't mention the subject,
  nonexistent realization names. Regex extraction plus lookups; semantic mismatches need a reader.

`tests/test_sweeps.py` pins known finds and tuned-away noise. Reports and the verified list live outside this repo
(`../sweeps/`, `../STALE_COMMENTS.md`).
