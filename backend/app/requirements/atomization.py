"""Conservative, traceable document statements for the new comparison flow."""
import json
import re
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


def _scope_key(
    number: str, section_path: str | None, fragment: Fragment,
) -> tuple[str, str, str, str | None] | None:
    """Only explicit section and, for tables, explicit table identity are safe."""
    if not section_path:
        return None
    if fragment.block_type == "table_row":
        if not fragment.container_id:
            return None
        return (number, section_path, "table", fragment.container_id)
    return (number, section_path, "prose", None)


def _section_ancestors(section_path: str | None) -> list[str]:
    if not section_path:
        return []
    parts = section_path.split(" / ")
    return [" / ".join(parts[:size]) for size in range(len(parts), 0, -1)]


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
    # Never index parents by bare number across a whole document: distinct
    # tables/sections frequently restart their own 1 / 1.1 numbering.
    scoped_parents: dict[tuple[str, str, str, str | None], DocumentAtom] = {}
    seen_numbers: set[str] = set()
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
        # Title labels remain traceable, but do not impose engineering scope.
        title_only = bool(preamble_sources) and all(
            re.fullmatch(
                r'(?:АО|ПАО|ООО)\s+«[^»\n]+»|Филиал\s+(?:АО|ПАО|ООО)\s+«[^»\n]+»\s*[—–-]\s*(?:[А-ЯЁ][а-яё-]+\s+){1,4}(?:ГРЭС|ГЭС|ТЭЦ)',
                line,
            )
            for source in preamble_sources for line in source.text.splitlines()
        )
        title_only = title_only and not any(
            re.search(r"\b(?:если|при|кроме|исключени\w*|долж\w*|необходим\w*|предусмотр\w*|только|либо|или)\b", source.text, re.IGNORECASE)
            for source in preamble_sources
        )
        uncertain_context = []
        if not title_only:
            uncertain_context.extend(preamble_sources)
        context += tuple(source for source in preamble_sources
                         if source.fragment_id not in {item.fragment_id for item in context})
        uncertain_context.extend(_source(fragment) for fragment in unscoped_headings)
        number_parts = clause.clause_number.split("#", 1)[0].split(".")
        parent_unresolved = False
        for length in range(1, len(number_parts)):
            parent_number = ".".join(number_parts[:length])
            parent = None
            for ancestor in _section_ancestors(clause.section_path):
                scope = _scope_key(parent_number, ancestor, first)
                if scope is not None:
                    parent = scoped_parents.get(scope)
                    if parent is not None:
                        break
            if parent is not None:
                uncertain_context.extend(parent.sources)
            elif parent_number in seen_numbers:
                # Numerically plausible parent exists elsewhere, but no
                # structural proof that it governs this clause. Never attach
                # its words or silently clear the need for manual review.
                parent_unresolved = True
        if uncertain_context:
            context_ids = {source.fragment_id for source in context}
            for source in uncertain_context:
                if source.fragment_id not in context_ids and source.fragment_id not in clause.fragment_ids:
                    context += (source,)
                    context_ids.add(source.fragment_id)
            warnings += ("context_scope_requires_review",)
        if parent_unresolved:
            warnings += ("parent_scope_unresolved", "context_scope_requires_review")
        warnings = tuple(dict.fromkeys(warnings))
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
        source_types = {by_id[source.fragment_id].block_type for source in sources}
        if "table_row" in source_types and len(source_types) > 1:
            # Unnumbered prose/notes may limit the preceding table. Keep
            # verbatim contents, but never auto-certify an uncertain boundary.
            warnings = tuple(dict.fromkeys((
                *warnings, "mixed_table_prose_scope_requires_review",
                "context_scope_requires_review",
            )))
        identity = json.dumps(["conservative-v1", document_id,
                               [asdict(source) for source in sources],
                               [asdict(source) for source in context]], ensure_ascii=False, sort_keys=True)
        atoms.append(DocumentAtom(str(uuid5(NAMESPACE_URL, identity)), document_id, clause.clause_number,
                                  clause.text, clause.section_path, tuple(sources), context, warnings))
        base_number = clause.clause_number.split("#", 1)[0]
        scope = _scope_key(base_number, clause.section_path, first)
        if scope is not None:
            scoped_parents[scope] = atoms[-1]
        seen_numbers.add(base_number)
        if clause.clause_number == "Преамбула":
            preamble_sources = tuple(sources)
    return atoms
