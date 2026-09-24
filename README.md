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
claude mcp add remind-rag --scope local -- uv run --offline --directory C:\Users\tonnru\lab\piam-rag\rag python -m remind_rag.server
```

Check with `/mcp` inside Claude Code. Tools:

| tool | use for |
|---|---|
| `search(query, k, module, realization, kind, phase, scenario)` | conceptual / free-text questions; `scenario` drops unselected realizations and down-ranks code compiled out by that scenario's switches |
| `get_symbol(name)` | exact GAMS identifiers: declaration, description + unit, providing/consuming modules, equation, use sites |
| `get_switch(name)` | `cm_*` / `c_*` switches and module selections: docs, default, allowed values, references |
| `get_module(module)` | description, realizations (+ default), limitations, interfaces (inputs/outputs) |
| `get_scenario(name)` | a scenario's settings and what differs from the `main.gms` defaults |

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
