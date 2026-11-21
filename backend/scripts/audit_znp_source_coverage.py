"""Offline audit of *all* parsed ZnP fragments, with optional private review.

No DeepSeek, embeddings, database, or networking. Default output contains
source IDs, positions, hashes and conservative role labels, never source text.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.core.models import Fragment
from app.documents.parser import extract_blocks
from app.requirements.atomization import atomize_fragments
from app.requirements.source_coverage import source_coverage_ledger


def audit_file(path: Path) -> tuple[dict, list[Fragment]]:
    raw = path.read_bytes()
    document_id = str(uuid5(
        NAMESPACE_URL, "parent-scope-audit:" + sha256(raw).hexdigest(),
    ))
    blocks = extract_blocks(raw, path.suffix.lower())
    fragments = [
        Fragment(
            id=str(uuid5(NAMESPACE_URL, f"{document_id}:{index}")),
            document_id=document_id,
            page=block.page, position=block.position,
            text=block.text, block_type=block.block_type,
            section_path=block.section_path,
            container_id=getattr(block, "container_id", None),
        ) for index, block in enumerate(blocks, 1)
    ]
    atoms = atomize_fragments(document_id, fragments)
    ledger = source_coverage_ledger(fragments, atoms)
    ledger["source_sha256"] = sha256(raw).hexdigest()
    ledger["source_atoms"] = [{
        "atom_id": atom.id, "clause_number": atom.clause_number,
        "source_ids": [source.fragment_id for source in atom.sources],
        "context_ids": [source.fragment_id for source in atom.context_sources],
        "warnings": list(atom.warnings),
    } for atom in atoms]
    return ledger, fragments


def _private_save(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(content)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--znp", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--private-review", type=Path,
        help="Opt-in local-only JSONL containing full text of unmatched fragments",
    )
    args = parser.parse_args(argv)
    ledger, fragments = audit_file(args.znp)
    _private_save(args.out, json.dumps(
        ledger, ensure_ascii=False, indent=2,
    ))
    if args.private_review is not None:
        pending_ids = set(ledger["unclassified_fragment_ids"])
        lines = []
        for fragment in fragments:
            if fragment.id in pending_ids:
                lines.append(json.dumps({
                    "fragment_id": fragment.id,
                    "page": fragment.page,
                    "position": fragment.position,
                    "block_type": fragment.block_type,
                    "section_path": fragment.section_path,
                    "container_id": getattr(fragment, "container_id", None),
                    "text": fragment.text,
                }, ensure_ascii=False))
        _private_save(args.private_review, "\n".join(lines) + "\n")
    print(json.dumps({
        "source_sha256": ledger["source_sha256"],
        "total_fragments": ledger["total_fragments"],
        "atoms": ledger["atoms"],
        "by_role": ledger["by_role"],
        "excluded_by_reason": ledger["excluded_by_reason"],
        "unclassified_by_block_type": ledger["unclassified_by_block_type"],
        "complete": ledger["complete"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
