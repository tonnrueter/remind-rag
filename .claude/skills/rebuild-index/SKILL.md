---
name: rebuild-index
description: Rebuild the REMIND RAG index after parser, enrichment or REMIND changes, and verify it with the parser checks, tool-output tests and the retrieval eval. Use when chunking/indexing code changed or the REMIND checkout moved on.
---

# Rebuild and verify the index

All local and free. A rebuild reuses every embedding whose exact text is unchanged (`--reuse`), so it takes seconds
to a minute; a full one (`--no-reuse`, a new model, or a change to `make_header`/`embed_text`) ~13 min on the laptop CPU.

1. **Versions.** Every index state is kept as `data/remind-bge-vN.db`; `data/remind-bge.db` is a copy of the
   current one (the MCP registration pins it). The new build becomes the next free N. Make sure the baseline has
   saved retrieval scores in `eval/results/` (`uv run python eval/retrieval_eval.py data/remind-bge-vN.db`).
2. **Quick check without embedding** (seconds): run the chunker plus `check_parse` from `remind_rag.index` on
   `iter_files(Path('../remind'))`, and expect an empty warning list. For changes that only touch `symbol_uses` or FTS:
   `cp data/remind-bge-v<current>.db data/remind-bge-v<N>.db`, then
   `uv run python -m remind_rag.index --root ..\remind --no-embed --db data/remind-bge-v<N>.db` (records
   `uses_rebuilt_at` in meta). Compare role counts and sample changed rows against the previous version.
3. **Rebuild**: `uv run python -m remind_rag.index --root ..\remind --model bge --db data/remind-bge-v<N>.db
   --reuse data/remind-bge-v<current>.db`. The log line `embeddings: X reused, Y to compute` shows the cost; run it
   in the background when Y is large.
   Add `--gms-export` only when REMIND's module structure changed; it needs R with gms/goxygen in `r/library`.
   The log must show `parser checks: 0 warning(s)` and `codeCheck declarations: …, missed by our parser: 0`.
4. **Verify** against the new file (the variable doesn't persist between shell calls, so set it on each command):
   - `REMIND_RAG_DB=data/remind-bge-v<N>.db uv run pytest tests`: all pass.
   - `uv run python eval/retrieval_eval.py data/remind-bge-v<N>.db`: hybrid recall@5 / MRR not below the baseline
     (81 % / 0.69 at v2 and v3, 2026-09-25). Save it under `eval/results/`.
5. **Promote:** `cp data/remind-bge-v<N>.db data/remind-bge.db`. Sessions pick it up on their next server start
   (Claude Code: `/mcp` → reconnect). Record the new numbers in `FINDINGS.md`.
