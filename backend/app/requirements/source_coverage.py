"""Source completeness ledger for conservative ZnP extraction.

Never infer that a parser fragment is safe to ignore just because it was
not extracted into an atom. Only unambiguously administrative/empty fragments
receive an excluded role; remaining gaps need manual classification.

The ledger contains provenance hashes and IDs, never private source text.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
from typing import Sequence

from app.core.models import Fragment
from app.requirements.atomization import DocumentAtom
from app.requirements.extraction import is_noise, is_toc_line


def _skip_reason(fragment: Fragment) -> str | None:
    text = (fragment.text or "").strip()
    if not text:
        return "empty"
    if fragment.block_type == "page_furniture":
        return "page_furniture"
    if fragment.block_type == "toc" or is_toc_line(text):
        return "toc"
    if text.casefold().strip(" .:") in ("оглавление", "содержание"):
        return "toc_heading"
    if is_noise(text):
        return "noise"
    return None


def source_coverage_ledger(
    fragments: Sequence[Fragment],
    atoms: Sequence[DocumentAtom],
) -> dict:
    """Classify each fragment as source, context, excluded, or unclassified.

    This audits the full *parser* output, not the semantic search candidates.
    No generated verdict or LLM response influences classification.
    """
    source_ids = {s.fragment_id for atom in atoms for s in atom.sources}
    context_ids = {s.fragment_id for atom in atoms for s in atom.context_sources}
    by_role = Counter()
    unclassified_by_block_type = Counter()
    records = []
    seen = set()
    for fragment in fragments:
        if fragment.id in seen:
            raise ValueError(f"Duplicate source fragment ID: {fragment.id}")
        seen.add(fragment.id)
        reason = None
        if fragment.id in source_ids:
            role = "atom_source"
        elif fragment.id in context_ids:
            role = "context_only"
        else:
            reason = _skip_reason(fragment)
            role = "excluded" if reason else "unclassified"
            if role == "unclassified":
                unclassified_by_block_type[fragment.block_type or "unknown"] += 1
        by_role[role] += 1
        records.append({
            "fragment_id": fragment.id,
            "page": fragment.page,
            "position": fragment.position,
            "block_type": fragment.block_type,
            "container_id": getattr(fragment, "container_id", None),
            "section_hash": sha256(
                (fragment.section_path or "").encode("utf-8")
            ).hexdigest(),
            "text_sha256": sha256(
                (fragment.text or "").encode("utf-8")
            ).hexdigest(),
            "role": role,
            "reason": reason,
        })
    missing = (source_ids | context_ids) - seen
    if missing:
        raise ValueError(
            f"Atom provenance refers to {len(missing)} absent parser fragments"
        )
    if sum(by_role.values()) != len(fragments):
        raise AssertionError("Source coverage must account for all parser fragments")
    return {
        "schema": "znp-source-coverage-v1",
        "total_fragments": len(fragments),
        "atoms": len(atoms),
        "by_role": dict(by_role),
        "unclassified_by_block_type": dict(unclassified_by_block_type),
        "unclassified_count": by_role["unclassified"],
        "complete": by_role["unclassified"] == 0,
        "records": records,
    }


def source_coverage_summary(ledger: dict) -> dict:
    """Compact report-safe summary, not a list of private fragment contents."""
    return {
        key: ledger[key] for key in (
            "schema", "total_fragments", "atoms", "by_role",
            "unclassified_by_block_type", "unclassified_count", "complete",
        )
    }
