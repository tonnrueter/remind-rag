---
name: rebuild-index
description: Rebuild the REMIND RAG index after parser, enrichment or REMIND changes, and verify it with the parser checks, tool-output tests and the retrieval eval. Use when chunking/indexing code changed or the REMIND checkout moved on.
---

# Rebuild and verify the index

All local and free; a full rebuild takes ~13 min on the laptop CPU.

1. **Baseline.** Keep the current index: `cp data/remind-bge.db data/remind-bge-vN.db` (next free N). If there is
   no saved baseline yet, record retrieval scores:
   `uv run python eval/retrieval_eval.py data/remind-bge-vN.db > eval/results/retrieval-vN.txt`.
2. **Quick check without embedding** (seconds): run the chunker plus `check_parse` from `remind_rag.index` on
   `iter_files(Path('../remind'))`, and expect an empty warning list. For changes that only touch `symbol_uses` or FTS:
   `uv run python -m remind_rag.index --root ..\remind --no-embed --db <copy of the index>`.
3. **Full rebuild** (in the background): `uv run python -m remind_rag.index --root ..\remind --model bge`.
   Add `--gms-export` only when REMIND's module structure changed; it needs R with gms/goxygen in `r/library`.
   The log must show `parser checks: 0 warning(s)` and `codeCheck declarations: …, missed by our parser: 0`.
4. **Verify:**
   - `uv run pytest tests`: all pass.
   - `uv run python eval/retrieval_eval.py data/remind-bge.db`: hybrid recall@5 / MRR not below the baseline
     (81 % / 0.69 at v2, 2026-09-25). Save it under `eval/results/`.
5. The MCP server picks the newest complete `data/remind-bge*.db` on its next start (Claude Code: `/mcp` →
   reconnect). Record the new numbers in `FINDINGS.md`.
