"""Ingest the gms/goxygen export (r/export_gms.R): module interfaces, limitations, not_used reasons."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .chunkers import MAX_CHARS, Chunk, path_meta

RAG_DIR = Path(__file__).resolve().parents[2]
EXPORT_SCRIPT = RAG_DIR / "r" / "export_gms.R"


def run_export(root: Path, out: Path) -> bool:
    """Run the R export; returns False (with a message) if R or the packages are not available."""
    try:
        subprocess.run(["Rscript", "--vanilla", str(EXPORT_SCRIPT), str(root), str(out)],
                       check=True, capture_output=True, text=True, timeout=600)
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        detail = getattr(e, "stderr", "") or str(e)
        print(f"WARNING: gms export failed, indexing without it:\n{detail[-1500:]}")
        return False


def load(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def module_folders(export: dict) -> dict[str, str]:
    """codeCheck module name (e.g. 'carbonRemoval') -> folder ('33_carbonRemoval'); core -> 'core'."""
    out = {m["name"]: m["folder"] for m in export["modules"]}
    out["core"] = "core"
    return out


def interface_map(export: dict) -> dict[str, dict[str, list[str]]]:
    """symbol -> {'provided_by': [folders], 'consumed_by': [folders]}"""
    folders = module_folders(export)
    out: dict[str, dict[str, list[str]]] = {}
    for row in export["interfaces"]:
        entry = out.setdefault(row["name"], {"provided_by": [], "consumed_by": []})
        key = "provided_by" if row["direction"] == "out" else "consumed_by"
        folder = folders.get(row["module"], row["module"])
        if folder not in entry[key]:
            entry[key].append(folder)
    return out


def _pack_lines(head: str, lines: list[str]) -> list[str]:
    texts, cur = [], head
    for line in lines:
        if len(cur) + len(line) + 1 > MAX_CHARS and cur != head:
            texts.append(cur)
            cur = head + " (continued)"
        cur += "\n" + line
    texts.append(cur)
    return texts


def module_interface_chunks(export: dict, sym_desc: dict[str, str]) -> list[Chunk]:
    """One (or a few) plain-English chunks per module: what it provides to and consumes from the rest."""
    iface = interface_map(export)
    folders = module_folders(export)
    chunks = []
    for name, folder in sorted(folders.items(), key=lambda kv: kv[1]):
        rows = [r for r in export["interfaces"] if r["module"] == name]
        if not rows:
            continue
        outs = sorted(r["name"] for r in rows if r["direction"] == "out")
        ins = sorted(r["name"] for r in rows if r["direction"] == "in")
        switches = [x for x in ins if x.startswith("c")]
        who = "the REMIND core" if folder == "core" else f"module {folder}"
        path = "core/declarations.gms" if folder == "core" else f"modules/{folder}/module.gms"
        meta = path_meta(path)
        # outputs and inputs in separate chunks, so "what does X need" and "what does X provide" each find
        # a focused chunk (one combined list is split arbitrarily once it exceeds MAX_CHARS)
        out_lines = [f"{v}: {sym_desc.get(v, '')[:70]} -> used by "
                     f"{', '.join(u for u in iface[v]['consumed_by'] if u != folder) or 'no other module'}"
                     for v in outs]
        in_lines = [f"{v}: {sym_desc.get(v, '')[:70]} <- from {', '.join(iface[v]['provided_by']) or '?'}"
                    for v in ins if not v.startswith("c")]
        if switches:
            in_lines.append("switches read: " + ", ".join(switches))
        for head, lines in ((f"Outputs of {who} (gms::codeCheck): {len(outs)} objects it provides to other modules",
                             out_lines),
                            (f"Inputs of {who} (gms::codeCheck): {len(ins)} objects it needs from other modules",
                             in_lines)):
            if not lines:
                continue
            for text in _pack_lines(head, lines):
                chunks.append(Chunk(path=path, kind="module_interface", line_start=1, line_end=1, text=text,
                                    name=folder, module=meta["module"], realization=None, phase="interfaces"))
    return chunks


def limitations_chunks(export: dict, root: Path) -> list[Chunk]:
    chunks = []
    for d in export["docs"]:
        if d["type"] != "limitations" or not d["text"].strip():
            continue
        lines = (root / d["path"]).read_text(encoding="utf-8", errors="replace").splitlines()
        line = next((i for i, x in enumerate(lines, 1) if "@limitations" in x), 1)
        meta = path_meta(d["path"])
        where = d["module"] + (f"/{meta['realization']}" if meta["realization"] else "")
        chunks.append(Chunk(path=d["path"], kind="limitations", line_start=line, line_end=line,
                            text=f"Limitations of {where}: {d['text']}", name=where, **meta))
    return chunks


def parser_diff(export: dict, our_names: set[str]) -> str:
    """Report declarations codeCheck sees that our regex parser misses (and vice versa)."""
    theirs = {d["names"] for d in export["declarations"] if d["type"] != "set"}
    missing = sorted(theirs - our_names)
    extra = sorted(n for n in our_names - theirs if "_" in n)
    return (f"codeCheck declarations: {len(theirs)}, missed by our parser: {len(missing)} {missing[:15]}; "
            f"only in ours (non-set, with '_'): {len(extra)} {extra[:10]}")
