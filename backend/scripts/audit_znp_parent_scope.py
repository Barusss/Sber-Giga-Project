"""Offline, text-free provenance audit for ZnP parent-scope changes.

Run independently on the same original ZnP file with the old and proposed
code. Use --compare-before to inspect which source-to-parent assignments
changed. Never invokes DeepSeek, embeddings, or database writes.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.core.models import Fragment
from app.documents.parser import extract_blocks
from app.requirements.atomization import atomize_fragments


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fragment_meta(fragment: Fragment) -> dict:
    return {
        "id": fragment.id,
        "page": fragment.page,
        "position": fragment.position,
        "kind": fragment.block_type,
        "section_hash": _hash(fragment.section_path or ""),
        "container_id": getattr(fragment, "container_id", None),
        "text_hash": _hash(fragment.text),
    }


def audit_znp(path: Path) -> dict:
    """Parse real source without API; never emit document or quote text."""
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    document_id = str(uuid5(NAMESPACE_URL, "parent-scope-audit:" + digest))
    blocks = extract_blocks(data, path.suffix.lower())
    fragments = []
    for i, block in enumerate(blocks, start=1):
        kwargs = dict(
            id=str(uuid5(NAMESPACE_URL, f"{document_id}:{i}")),
            document_id=document_id,
            page=block.page,
            position=block.position,
            text=block.text,
            block_type=block.block_type,
            section_path=block.section_path,
        )
        # This script intentionally runs on BOTH pre-change dev and PR #53.
        if hasattr(Fragment, "container_id"):
            kwargs["container_id"] = getattr(block, "container_id", None)
        fragments.append(Fragment(**kwargs))
    atoms = atomize_fragments(document_id, fragments)
    by_id = {f.id: f for f in fragments}
    owner = {
        source.fragment_id: atom for atom in atoms for source in atom.sources
    }
    records = []
    scope_anomalies = []
    for atom in atoms:
        source_ids = [source.fragment_id for source in atom.sources]
        context_ids = [source.fragment_id for source in atom.context_sources]
        parents = []
        for context_id in context_ids:
            parent = owner.get(context_id)
            if parent is None or parent.id == atom.id or parent.clause_number == "Преамбула":
                continue
            if any(p["atom_id"] == parent.id for p in parents):
                continue
            first_child = by_id[source_ids[0]]
            first_parent = by_id[parent.sources[0].fragment_id]
            different_tables = (
                first_child.block_type == "table_row"
                and first_parent.block_type == "table_row"
                and bool(getattr(first_child, "container_id", None))
                and bool(getattr(first_parent, "container_id", None))
                and first_child.container_id != first_parent.container_id
            )
            different_sections = (
                bool(atom.section_path and parent.section_path)
                and parent.section_path != atom.section_path
                and not atom.section_path.startswith(parent.section_path + " / ")
            )
            parent_ref = {
                "atom_id": parent.id,
                "clause_number": parent.clause_number,
                "source_ids": [s.fragment_id for s in parent.sources],
                "different_tables": different_tables,
                "different_sections": different_sections,
            }
            parents.append(parent_ref)
            if different_tables or different_sections:
                scope_anomalies.append({
                    "atom_id": atom.id,
                    "clause_number": atom.clause_number,
                    "parent_atom_id": parent.id,
                    "different_tables": different_tables,
                    "different_sections": different_sections,
                })
        records.append({
            "atom_id": atom.id,
            "clause_number": atom.clause_number,
            "source_key": _hash(json.dumps(source_ids)),
            "source_ids": source_ids,
            "context_ids": context_ids,
            "parent_refs": parents,
            "warnings": list(atom.warnings),
        })
    return {
        "format": "parent-scope-audit-v1",
        "source_sha256": digest,
        "document_id": document_id,
        "summary": {
            "fragments": len(fragments),
            "atoms": len(atoms),
            "table_fragments": sum(f.block_type == "table_row" for f in fragments),
            "tables_identified": len({
                getattr(f, "container_id", None) for f in fragments
                if f.block_type == "table_row"
                and getattr(f, "container_id", None)
            }),
            "warnings": dict(Counter(
                warning for atom in atoms for warning in atom.warnings
            )),
            "atoms_with_context": sum(bool(a.context_sources) for a in atoms),
            "atoms_with_parent_refs": sum(bool(row["parent_refs"]) for row in records),
            "scope_anomalies": len(scope_anomalies),
        },
        "scope_anomalies": scope_anomalies,
        "fragments": [_fragment_meta(f) for f in fragments],
        "atoms": records,
    }


def compare_audits(before: dict, after: dict) -> dict:
    """Compare by source fragments, never by unstable atom IDs."""
    if before["source_sha256"] != after["source_sha256"]:
        raise ValueError("Cannot compare audits of different source files")
    old_atoms = {a["source_key"]: a for a in before["atoms"]}
    new_atoms = {a["source_key"]: a for a in after["atoms"]}
    common = sorted(old_atoms.keys() & new_atoms.keys())
    changed = []
    for source_key in common:
        old, new = old_atoms[source_key], new_atoms[source_key]
        if (old["context_ids"] != new["context_ids"]
                or old["warnings"] != new["warnings"]):
            changed.append({
                "source_key": source_key,
                "clause_number": new["clause_number"],
                "old_context_ids": old["context_ids"],
                "new_context_ids": new["context_ids"],
                "old_warnings": old["warnings"],
                "new_warnings": new["warnings"],
            })
    old_fragment_owners = {
        f: sorted(a["source_ids"]) for a in before["atoms"] for f in a["source_ids"]
    }
    new_fragment_owners = {
        f: sorted(a["source_ids"]) for a in after["atoms"] for f in a["source_ids"]
    }
    common_fragments = old_fragment_owners.keys() & new_fragment_owners.keys()
    regrouped = sorted(
        f for f in common_fragments
        if old_fragment_owners[f] != new_fragment_owners[f]
    )
    return {
        "format": "parent-scope-diff-v1",
        "source_sha256": after["source_sha256"],
        "before": before["summary"],
        "after": after["summary"],
        "unchanged_source_groups": len(common),
        "removed_source_groups": len(old_atoms.keys() - new_atoms.keys()),
        "new_source_groups": len(new_atoms.keys() - old_atoms.keys()),
        "changed_context_or_warnings": changed,
        "regrouped_fragment_ids": regrouped,
        "lost_source_fragment_ids": sorted(
            old_fragment_owners.keys() - new_fragment_owners.keys()
        ),
        "new_source_fragment_ids": sorted(
            new_fragment_owners.keys() - old_fragment_owners.keys()
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--znp", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--compare-before", type=Path)
    args = parser.parse_args(argv)
    audit = audit_znp(args.znp)
    result = audit
    if args.compare_before is not None:
        previous = json.loads(args.compare_before.read_text(encoding="utf-8"))
        result = compare_audits(previous, audit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    summary = (
        {"before": result["before"], "after": result["after"],
         "regrouped_fragments": len(result["regrouped_fragment_ids"]),
         "context_changes": len(result["changed_context_or_warnings"]),
         "lost_source_fragments": len(result["lost_source_fragment_ids"])}
        if args.compare_before else result["summary"]
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
