"""What a rebuild changed in the index contents: rows added and removed per table, the where-used table line by
line, and the uses of chosen symbols before and after. Free, local, no model.

    uv run python eval/db_diff.py data/remind-bge-v10.db data/remind-bge-v11.db [--symbol pm_damage ...]

Compare two builds of the same REMIND commit (meta.remind_commit), otherwise REMIND's own changes mix with ours.
The keyword and vector indexes are not compared here; eval/retrieval_eval.py measures what they are for.
"""

from __future__ import annotations

import argparse
from collections import Counter

from remind_rag import store

# where-used rows are identified by symbol, file and line; a row with the same key and other values is "changed"
USE_KEY = ("name", "path", "line")


def tables(db) -> list[str]:
    """The plain tables, without the FTS5 / sqlite-vec tables and their shadow tables."""
    rows = db.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY rowid")
    return [n for n, sql in rows if not n.startswith(("chunks_fts", "chunks_vec", "sqlite_")) and "VIRTUAL" not in sql]


def rows(db, table: str) -> tuple[list[str], list[tuple]]:
    cur = db.execute(f"SELECT * FROM {table}")
    return [d[0] for d in cur.description], [tuple(r) for r in cur]


def short(row: dict, width: int = 110) -> str:
    text = " | ".join(f"{k}={v}" for k, v in row.items() if v not in (None, "", "[]"))
    return text if len(text) <= width else text[: width - 1] + "..."


def diff_meta(old, new) -> None:
    a, b = dict(old.execute("SELECT key, value FROM meta")), dict(new.execute("SELECT key, value FROM meta"))
    if a.get("remind_commit") != b.get("remind_commit"):
        commits = " vs ".join(str(m.get("remind_commit"))[:9] for m in (a, b))
        print(f"WARNING: different REMIND commits ({commits})")
    print("meta (changed keys):")
    for k in sorted(a.keys() | b.keys()):
        if a.get(k) != b.get(k):
            print(f"  {k}: {str(a.get(k))[:60]} -> {str(b.get(k))[:60]}")


def diff_table(old, new, table: str, examples: int) -> None:
    """Rows compared as a multiset of whole rows: a row present twice must be present twice afterwards."""
    cols, a = rows(old, table)
    _, b = rows(new, table)
    ca, cb = Counter(a), Counter(b)
    added, removed = list((cb - ca).elements()), list((ca - cb).elements())
    mark = "identical" if not added and not removed else f"+{len(added)} / -{len(removed)}"
    print(f"\n{table}: {len(a):,} -> {len(b):,} rows, {mark}")
    for sign, part in (("+", added), ("-", removed)):
        for r in part[:examples]:
            print(f"  {sign} {short(dict(zip(cols, r, strict=True)))}")
        if len(part) > examples:
            print(f"  {sign} ... {len(part) - examples} more")


def diff_uses(old, new, examples: int) -> None:
    """symbol_uses by (name, path, line): added, removed, and changed (same line, other role / context / guards)."""
    cols, a = rows(old, "symbol_uses")
    _, b = rows(new, "symbol_uses")
    by_key = [
        {tuple(r[cols.index(k)] for k in USE_KEY): dict(zip(cols, r, strict=True)) for r in part} for part in (a, b)
    ]
    ka, kb = by_key
    added = [kb[k] for k in kb.keys() - ka.keys()]
    removed = [ka[k] for k in ka.keys() - kb.keys()]
    changed = [(ka[k], kb[k]) for k in ka.keys() & kb.keys() if ka[k] != kb[k]]
    print(f"\nsymbol_uses: {len(a):,} -> {len(b):,} rows (+{len(added)} / -{len(removed)} / {len(changed)} changed)")
    for sign, part in (("+", added), ("-", removed)):
        if not part:
            continue
        part.sort(key=lambda r: (r["path"], r["line"], r["name"]))
        print(f"  {sign} by role: {dict(Counter(r['role'] for r in part).most_common())}")
        print(f"  {sign} by phase: {dict(Counter(r['phase'] for r in part).most_common())}")
        print(f"  {sign} files: {len({r['path'] for r in part})}, symbols: {len({r['name'] for r in part})}")
        for path, n in Counter(r["path"] for r in part).most_common(examples):
            print(f"      {n:4}  {path}")
        for r in part[:examples]:
            print(f"  {sign} {r['name']}  {r['path']}:{r['line']}  {r['role']}  | {r['snippet'][:60]}")
    for r_old, r_new in sorted(changed, key=lambda p: (p[0]["path"], p[0]["line"]))[:examples]:
        cols_changed = [c for c in cols if r_old[c] != r_new[c]]
        print(
            f"  ~ {r_old['name']}  {r_old['path']}:{r_old['line']}  "
            + ", ".join(f"{c}: {r_old[c]} -> {r_new[c]}" for c in cols_changed)
        )


def diff_symbol(old, new, name: str) -> None:
    """One symbol's uses as get_symbol sees them: file, line, role and equation, before and after."""
    query = "SELECT path, line, role, context FROM symbol_uses WHERE name = ? COLLATE NOCASE ORDER BY path, line"
    a, b = (set(map(tuple, db.execute(query, (name,)))) for db in (old, new))
    print(f"\n{name}: {len(a)} -> {len(b)} uses")
    for sign, part in (("+", b - a), ("-", a - b)):
        for path, line, role, ctx in sorted(part):
            print(f"  {sign} {path}:{line}  {role}" + (f"  in {ctx}" if ctx else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--symbol", action="append", default=[], help="also list this symbol's uses before/after")
    ap.add_argument("--examples", type=int, default=10, help="rows shown per list (default 10)")
    args = ap.parse_args()
    old, new = store.connect(args.old), store.connect(args.new)
    diff_meta(old, new)
    for table in tables(new):
        if table == "symbol_uses":
            diff_uses(old, new, args.examples)
        elif table != "meta":
            diff_table(old, new, table, args.examples)
    for name in args.symbol:
        diff_symbol(old, new, name)


if __name__ == "__main__":
    main()
