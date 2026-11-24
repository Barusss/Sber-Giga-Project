"""Conservative, traceable document statements for the new comparison flow."""
import json
import re
from dataclasses import dataclass, asdict, replace
from pathlib import Path
from typing import Sequence
from uuid import NAMESPACE_URL, uuid5

from app.core.models import Fragment
from app.documents.parser import TABLE_REVIEW, requires_pdf_layout_review
from app.requirements.extraction import extract_clauses, is_table_header, is_table_description, is_table_caption


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
    heading_id: str | None,
) -> tuple[str, str, str, str | None, str | None] | None:
    """Only explicit section and, for tables, explicit table identity are safe."""
    if not section_path:
        return None
    if fragment.block_type == "table_row":
        if not fragment.container_id:
            return None
        return (number, section_path, "table", fragment.container_id, heading_id)
    return (number, section_path, "prose", None, heading_id)


def _section_ancestors(section_path: str | None) -> list[str]:
    if not section_path:
        return []
    parts = section_path.split(" / ")
    return [" / ".join(parts[:size]) for size in range(len(parts), 0, -1)]


def atomize_document(document, fragments):
    path = Path(document.stored_path)
    data = path.read_bytes() if path.suffix.lower() == ".pdf" and path.is_file() else None
    return atomize_fragments(document.id, fragments, pdf_data=data,
                            unverified_pdf=requires_pdf_layout_review(document))


def atomize_fragments(document_id: str, fragments: Sequence[Fragment], *, pdf_data: bytes | None = None,
                      unverified_pdf: bool = False) -> list[DocumentAtom]:
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
    table_context = {}
    for index, fragment in enumerate(ordered):
        previous = ordered[index - 1] if index else None
        following = ordered[index + 1] if index + 1 < len(ordered) else None
        if fragment.block_type == "paragraph" and is_table_caption(fragment.text):
            neighbors = {
                neighbor.container_id for neighbor in (previous, following)
                if neighbor is not None and neighbor.block_type == "table_row"
                and neighbor.container_id and neighbor.section_path == fragment.section_path
            }
            # Between distinct tables the ownership is not proved. Leave the
            # caption unclassified rather than attach it to both containers.
            if len(neighbors) == 1:
                table_context.setdefault(next(iter(neighbors)), []).append(_source(fragment))
        if (fragment.block_type == "table_row" and fragment.container_id
                and is_table_header(fragment.text)):
            context = table_context.setdefault(fragment.container_id, [])
            context.append(_source(fragment))
            if (previous is not None and previous.block_type == "paragraph"
                    and previous.section_path == fragment.section_path
                    and is_table_description(previous.text)):
                context.append(_source(previous))
    headings = {}
    unscoped_headings = []
    # Never index parents by bare number across a whole document: distinct
    # tables/sections frequently restart their own 1 / 1.1 numbering.
    scoped_parents: dict[
        tuple[str, str, str, str | None, str | None], DocumentAtom
    ] = {}
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
        # A header describes columns only within its proven table container.
        # These labels are structural metadata, not uncertain obligations.
        context += tuple(table_context.get(first.container_id, ()))
        warnings = clause.warnings
        if clause.section_path and clause.section_path not in headings:
            warnings += ("section_context_unresolved",)
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
                heading = headings.get(ancestor)
                scope = _scope_key(
                    parent_number, ancestor, first,
                    heading.id if heading is not None else None,
                )
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
        if clause.section_path and re.search(
            r"(?:^| / )приложение\s+(?:№\s*)?(?:\d{1,3}|[А-ЯЁA-Z])(?=[\s.:—–-]|$)",
            clause.section_path, re.IGNORECASE,
        ):
            # Appendix table cells may encode parameter/exception context.
            # Preserving them for matching does NOT prove scope and cannot
            # create an automatic engineering confirmation.
            warnings = tuple(dict.fromkeys((
                *warnings, "appendix_scope_requires_review",
                "context_scope_requires_review",
            )))
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
        if any((by_id[s.fragment_id].container_id or "").startswith(TABLE_REVIEW)
               for s in (*sources, *context)):
            warnings += (TABLE_REVIEW,)
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
        heading = headings.get(clause.section_path)
        scope = _scope_key(
            base_number, clause.section_path, first,
            heading.id if heading is not None else None,
        )
        if scope is not None:
            scoped_parents[scope] = atoms[-1]
        seen_numbers.add(base_number)
        if clause.clause_number == "Преамбула":
            preamble_sources = tuple(sources)
    if unverified_pdf:
        atoms = [replace(atom, warnings=tuple(dict.fromkeys((*atom.warnings, TABLE_REVIEW))))
                 for atom in atoms]
    if pdf_data is not None:
        from app.requirements.pdf_cell_sources import bind_pdf_cells

        cells = bind_pdf_cells(pdf_data, fragments)
        rows = {}
        touched = set()
        for page, cell, sources, reason in cells:
            if reason is None:
                rows.setdefault((page, cell.row), {})[cell.column] = sources
                touched.update(source.fragment_id for source in sources)
        if touched:
            # Existing whole-fragment groups could include neighbouring rows.
            # Keep their uncovered material visible to the coverage ledger.
            atoms = [atom for atom in atoms if not touched.intersection(
                source.fragment_id for source in (*atom.sources, *atom.context_sources)
            )]
        for (page, row), columns in rows.items():
            if set(columns) != {0, 1}:
                continue
            sources = (*columns[0], *columns[1])
            text = "\n".join(source.text for source in sources)
            number_match = re.match(r"^\s*(\d+(?:\.\d+)*)", columns[0][0].text)
            number = number_match.group(1) if number_match else f"Строка {row}"
            identity = json.dumps(["pdf-grid-v1", document_id, page, row,
                                   [asdict(source) for source in sources]],
                                  ensure_ascii=False, sort_keys=True)
            atoms.append(DocumentAtom(
                str(uuid5(NAMESPACE_URL, identity)), document_id, number, text,
                None, sources, (), (),
            ))
    return atoms
