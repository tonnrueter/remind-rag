"""Retrieval quality: recall@5 and MRR@10 per ranking mode, index and question type.

    uv run python eval/retrieval_eval.py data/remind-bge-v0.db data/remind-jina-v0.db
"""

from __future__ import annotations

import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml

from remind_rag.search import Index

QUESTIONS = yaml.safe_load((Path(__file__).parent / "questions.yaml").read_text(encoding="utf-8"))
MODES = ["bm25", "vector", "hybrid"]


def first_hit(rows, q) -> int | None:
    needle = (q.get("must_contain") or "").lower()
    for rank, r in enumerate(rows, 1):
        if r["path"] in q["gold"] and (not needle or needle in (r["header"] + r["text"]).lower()):
            return rank
    return None


def main(db_paths: list[str]) -> None:
    for db_path in db_paths:
        idx = Index(db_path)
        idx.vector("warm up", 1)  # load the model outside the timing
        print(f"\n=== {db_path}  (model={idx.model}, chunks={idx.meta['chunks']})")
        table = defaultdict(lambda: defaultdict(list))  # mode -> type -> ranks
        misses = defaultdict(list)
        latency = defaultdict(list)
        for q in QUESTIONS:
            for mode in MODES:
                t = time.perf_counter()
                rows = idx.search(q["question"], k=10, mode=mode)
                latency[mode].append(time.perf_counter() - t)
                rank = first_hit(rows, q)
                table[mode][q["type"]].append(rank)
                table[mode]["ALL"].append(rank)
                if mode == "hybrid" and (rank is None or rank > 5):
                    misses[q["id"]] = [f"{r['path']}:{r['line_start']} ({r['kind']})" for r in rows[:3]]

        types = sorted({q["type"] for q in QUESTIONS}) + ["ALL"]
        print(f"{'mode':8s} " + " ".join(f"{t:>14s}" for t in types) + "   median latency")
        for mode in MODES:
            cells = []
            for t in types:
                ranks = table[mode][t]
                recall = sum(1 for r in ranks if r and r <= 5) / len(ranks)
                mrr = sum(1 / r for r in ranks if r) / len(ranks)
                cells.append(f"{recall:4.0%} / {mrr:4.2f}")
            lat = sorted(latency[mode])[len(latency[mode]) // 2] * 1000
            print(f"{mode:8s} " + " ".join(f"{c:>14s}" for c in cells) + f"   {lat:6.0f} ms")
        print("(cells: recall@5 / MRR@10)")
        if misses:
            print("hybrid misses (top-3 shown):")
            for qid, top in misses.items():
                print(f"  {qid}: {top}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["data/remind-bge-v0.db"])
