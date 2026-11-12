"""Conservative, traceable document statements for the new comparison flow."""
import json
from dataclasses import dataclass, asdict
from typing import Sequence
from uuid import NAMESPACE_URL, uuid5

from app.core.models import Fragment
from app.requirements.extraction import extract_clauses


@dataclass(frozen=True)
class AtomSource:
    fragment_id: str
    page: int
    start_offset: int
    end_offset: int
    text: str


@dataclass(frozen=True)
class DocumentAtom:
    id: str
    document_id: str
    clause_number: str
    text: str
    section_path: str | None
    sources: tuple[AtomSource, ...]
    context_sources: tuple[AtomSource, ...]
    warnings: tuple[str, ...]


def _source(fragment: Fragment) -> AtomSource:
    text = fragment.text.strip()
    start = fragment.text.find(text)
    return AtomSource(fragment.id, fragment.page, start, start + len(text), text)


def atomize_fragments(document_id: str, fragments: Sequence[Fragment]) -> list[DocumentAtom]:
    """Keep each extracted clause whole; reject unverifiable source mappings."""
    by_id = {}
    for fragment in fragments:
        if fragment.document_id != document_id or not fragment.id:
            raise ValueError("Every fragment must have an ID and belong to the document")
        if fragment.id in by_id:
            raise ValueError("Duplicate fragment ID")
        by_id[fragment.id] = fragment

    atoms = []
    ordered = sorted(fragments, key=lambda fragment: (fragment.page, fragment.position))
    headings = {}
    unscoped_headings = []
    previous_clauses = {}
    preamble_sources = ()
    fragment_cursor = 0
    for clause in extract_clauses(fragments, conservative=True):
        first = by_id[clause.fragment_ids[0]]
        while fragment_cursor < len(ordered):
            fragment = ordered[fragment_cursor]
            if (fragment.page, fragment.position) > (first.page, first.position):
                break
            if fragment.block_type == "heading":
                if fragment.section_path:
                    headings[fragment.section_path] = fragment
                    unscoped_headings = []
                else:
                    unscoped_headings.append(fragment)
            fragment_cursor += 1
        context = tuple(_source(fragment) for path, fragment in headings.items()
                        if clause.section_path and (path == clause.section_path
                        or clause.section_path.startswith(path + " / ")))
        warnings = ()
        if clause.section_path and clause.section_path not in headings:
            warnings = ("section_context_unresolved",)
        uncertain_context = list(preamble_sources)
        uncertain_context.extend(_source(fragment) for fragment in unscoped_headings)
        number_parts = clause.clause_number.split("#", 1)[0].split(".")
        for length in range(1, len(number_parts)):
            parent = previous_clauses.get(".".join(number_parts[:length]))
            if parent is not None:
                uncertain_context.extend(parent.sources)
        if uncertain_context:
            context_ids = {source.fragment_id for source in context}
            for source in uncertain_context:
                if source.fragment_id not in context_ids and source.fragment_id not in clause.fragment_ids:
                    context += (source,)
                    context_ids.add(source.fragment_id)
            warnings += ("context_scope_requires_review",)
        sources = []
        cursor = 0
        for fragment_id in clause.fragment_ids:
            fragment = by_id[fragment_id]
            source_text = fragment.text.strip()
            start = fragment.text.find(source_text)
            position = clause.text.find(source_text, cursor)
            if not source_text or start < 0 or position < 0:
                raise ValueError("Cannot verify atom text against source fragment")
            sources.append(AtomSource(fragment_id, fragment.page, start, start + len(source_text), source_text))
            cursor = position + len(source_text)
        if not sources or "\n".join(source.text for source in sources) != clause.text:
            raise ValueError("Atom text is not completely covered by source fragments")
        identity = json.dumps(["conservative-v1", document_id,
                               [asdict(source) for source in sources],
                               [asdict(source) for source in context]], ensure_ascii=False, sort_keys=True)
        atoms.append(DocumentAtom(str(uuid5(NAMESPACE_URL, identity)), document_id, clause.clause_number,
                                  clause.text, clause.section_path, tuple(sources), context, warnings))
        previous_clauses[clause.clause_number.split("#", 1)[0]] = atoms[-1]
        if clause.clause_number == "Преамбула":
            preamble_sources = tuple(sources)
    return atoms
