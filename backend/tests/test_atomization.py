import pytest

from app.core.models import Fragment
from app.requirements.atomization import atomize_fragments


def fragment(id, page, text, document_id="doc"):
    return Fragment(id=id, document_id=document_id, page=page, position=0,
                    text=text, block_type="paragraph", section_path="Раздел 1")


def test_atom_preserves_condition_sources_and_stable_id():
    fragments = [fragment("a", 1, "  1.1. Кабель допускается 95 мм²  "),
                 fragment("b", 2, "при условии прокладки двух параллельных линий.")]
    atom = atomize_fragments("doc", fragments)[0]
    assert atom.text == "1.1. Кабель допускается 95 мм²\nпри условии прокладки двух параллельных линий."
    assert [source.fragment_id for source in atom.sources] == ["a", "b"]
    assert all(fragments[i].text[source.start_offset:source.end_offset] == source.text
               for i, source in enumerate(atom.sources))
    assert atom.id == atomize_fragments("doc", fragments)[0].id


def test_duplicate_text_has_distinct_ids():
    atoms = atomize_fragments("doc", [fragment("a", 1, "1.1. Предусмотреть защиту."),
                                      fragment("b", 2, "1.1. Предусмотреть защиту.")])
    assert len(atoms) == 2
    assert atoms[0].id != atoms[1].id


def test_invalid_fragment_provenance_is_rejected():
    with pytest.raises(ValueError):
        atomize_fragments("doc", [fragment("a", 1, "1.1. Текст", document_id="other")])


def test_long_clause_is_not_split_and_retains_restriction():
    text = "1.1. " + "Кабельная линия. " * 300 + "Допускается при условии установки защиты."
    atoms = atomize_fragments("doc", [fragment("a", 1, text)])
    assert len(atoms) == 1
    assert atoms[0].text == text
    assert atoms[0].sources[0].text == text


def test_changed_source_content_changes_atom_id():
    original = atomize_fragments("doc", [fragment("a", 1, "1.1. Мощность 10 кВт.")])[0]
    changed = atomize_fragments("doc", [fragment("a", 1, "1.1. Мощность 20 кВт.")])[0]
    assert original.id != changed.id


def test_short_unnumbered_obligation_is_not_lost():
    atoms = atomize_fragments("doc", [fragment("a", 1, "Установить трансформатор.")])
    assert len(atoms) == 1
    assert atoms[0].text == "Установить трансформатор."


def test_numbered_children_keep_parent_condition():
    fragments = [fragment("a", 1, "1. При наличии двух линий допускается:"),
                 fragment("b", 2, "1.1. Кабель 95 мм²."),
                 fragment("c", 3, "1.2. Кабель 120 мм²."),
                 fragment("d", 4, "2. Предусмотреть учёт.")]
    atoms = atomize_fragments("doc", fragments)
    assert len(atoms) == 2
    assert [s.fragment_id for s in atoms[0].sources] == ["a", "b", "c"]
    assert "При наличии двух линий" in atoms[0].text


def test_heading_context_has_exact_source_and_changes_identity():
    heading = fragment("h", 1, "1. При наличии двух линий")
    heading.block_type = "heading"
    heading.section_path = heading.text
    body = fragment("a", 2, "1.1. Допускается кабель 95 мм².")
    body.section_path = heading.text
    original = atomize_fragments("doc", [heading, body])[0]
    assert original.context_sources[0].text == heading.text
    heading.text = "1. При наличии трёх линий"
    heading.section_path = heading.text
    body.section_path = heading.text
    changed = atomize_fragments("doc", [heading, body])[0]
    assert original.id != changed.id


def test_context_does_not_leak_from_previous_section():
    first = fragment("h1", 1, "1. Условие для первой линии")
    first.block_type = "heading"
    first.section_path = first.text
    second = fragment("h2", 2, "2. Условие для второй линии")
    second.block_type = "heading"
    second.section_path = second.text
    body = fragment("b", 3, "2.1. Предусмотреть защиту.")
    body.section_path = second.text
    atom = atomize_fragments("doc", [first, second, body])[0]
    assert [s.fragment_id for s in atom.context_sources] == ["h2"]


@pytest.mark.parametrize("text", [
    "1.1. Для КЛ-1 предусмотреть алюминиевый кабель не менее 120 мм² на 10 кВ.",
    "1.1. Допускается кабель 95 мм² при условии прокладки двух параллельных линий.",
    "1.1. Для всех линий, за исключением КЛ-2, запрещается применение меди.",
    "1.1. Предусмотреть кабель 120 мм² или две линии по 95 мм².",
])
def test_constraints_and_alternatives_remain_verbatim(text):
    atom = atomize_fragments("doc", [fragment("a", 1, text)])[0]
    assert atom.text == text
    assert atom.sources[0].text == text


def test_hundred_clauses_and_input_reordering():
    fragments = [fragment(str(i), i, f"1.1. Предусмотреть защиту оборудования {i}.")
                 for i in range(1, 101)]
    atoms = atomize_fragments("doc", fragments)
    assert len(atoms) == 100
    assert len({atom.id for atom in atoms}) == 100
    assert sum(len(atom.sources) for atom in atoms) == 100
    assert atoms == atomize_fragments("doc", list(reversed(fragments)))


def test_empty_document_and_duplicate_fragment_ids():
    assert atomize_fragments("doc", []) == []
    with pytest.raises(ValueError, match="Duplicate fragment ID"):
        atomize_fragments("doc", [fragment("a", 1, "1.1. Кабель."), fragment("a", 2, "1.2. Учёт.")])


def test_trailing_note_condition_remains_with_permission():
    note = fragment("n", 2, "Примечание")
    note.block_type = "heading"
    atoms = atomize_fragments("doc", [fragment("a", 1, "1.1. Допускается кабель 95 мм²."),
                                      note, fragment("b", 3, "Только при наличии двух линий.")])
    assert "Только при наличии двух линий." in atoms[0].text
    assert [s.fragment_id for s in atoms[0].sources] == ["a", "n", "b"]


def test_preamble_is_traceable_context_of_following_permission():
    atoms = atomize_fragments("doc", [fragment("a", 1, "При наличии двух линий:"),
                                      fragment("b", 2, "1.1. Допускается кабель 95 мм².")])
    assert any(s.fragment_id == "a" for s in atoms[-1].context_sources)


def test_heading_without_path_is_preserved_as_uncertain_context():
    heading = fragment("h", 1, "При наличии двух линий")
    heading.block_type = "heading"
    heading.section_path = None
    body = fragment("b", 2, "1.1. Допускается кабель 95 мм².")
    body.section_path = None
    atom = atomize_fragments("doc", [heading, body])[0]
    assert atom.context_sources[0].fragment_id == "h"
    assert "context_scope_requires_review" in atom.warnings


def test_parent_condition_survives_intervening_child_heading():
    parent = fragment("p", 1, "1. При наличии двух линий допускается:")
    heading = fragment("h", 2, "1.1. Кабели")
    heading.block_type = "heading"
    heading.section_path = "Раздел 1 / " + heading.text
    body = fragment("b", 3, "1.1.1. Кабель 95 мм².")
    body.section_path = heading.section_path
    child = atomize_fragments("doc", [parent, heading, body])[-1]
    assert {source.fragment_id for source in child.context_sources} == {"p", "h"}
    assert "context_scope_requires_review" in child.warnings


def test_unscoped_heading_chain_preserves_both_sources():
    first = fragment("h1", 1, "При наличии двух линий")
    second = fragment("h2", 2, "Кабели")
    for heading in (first, second):
        heading.block_type = "heading"
        heading.section_path = None
    body = fragment("b", 3, "1.1. Кабель 95 мм².")
    body.section_path = None
    atom = atomize_fragments("doc", [first, second, body])[0]
    assert {s.fragment_id for s in atom.context_sources} == {"h1", "h2"}


def test_duplicate_parent_number_uses_latest_preceding_parent():
    fragments = []
    for i, count in enumerate(("двух", "трёх")):
        fragments.append(fragment(f"p{i}", i * 3 + 1, f"1. При наличии {count} линий:"))
        heading = fragment(f"h{i}", i * 3 + 2, "1.1. Кабели")
        heading.block_type = "heading"
        heading.section_path = heading.text
        fragments.append(heading)
        fragments.append(fragment(f"b{i}", i * 3 + 3, "1.1.1. Кабель 95 мм²."))
    child = atomize_fragments("doc", fragments)[-1]
    ids = {s.fragment_id for s in child.context_sources}
    assert "p1" in ids
    assert "p0" not in ids


def test_title_preamble_keeps_provenance_without_poisoning_all_clauses():
    title = fragment("title", 1, "АО «ТГК-16»\nФилиал АО «ТГК-16» — Уруссинская ГРЭС")
    heading = fragment("h", 2, "1. Кабельные линии")
    heading.block_type = "heading"
    heading.section_path = "Кабели"
    body = fragment("b", 3, "1.1. Для КЛ-1 предусмотреть кабель 10 кВ.")
    body.section_path = "Кабели"
    atoms = atomize_fragments("doc", [title, heading, body])
    child = atoms[-1]
    assert "context_scope_requires_review" not in child.warnings
    assert "title" in {s.fragment_id for s in child.context_sources}
    assert child.sources[0].fragment_id == "b"


@pytest.mark.parametrize("preamble", [
    "При наличии двух линий допускается:",
    "Для КЛ-1 или КЛ-2 предусмотреть:",
    "Для всех линий, за исключением КЛ-2:",
    "АО «ТГК-16»\nТолько при наличии двух линий:",
])
def test_meaningful_preamble_still_requires_scope_review(preamble):
    heading = fragment("h", 2, "1. Кабели")
    heading.block_type = "heading"
    heading.section_path = "Кабели"
    body = fragment("b", 3, "1.1. Кабель 95 мм².")
    body.section_path = "Кабели"
    child = atomize_fragments("doc", [fragment("p",1,preamble),heading,body])[-1]
    assert "context_scope_requires_review" in child.warnings
    assert "p" in {s.fragment_id for s in child.context_sources}


def test_different_sections_cannot_inherit_same_number_parent():
    first = fragment("parent", 1, "1. Только при наличии КЛ-2.", document_id="doc")
    first.section_path = "Раздел А"
    child = fragment("child", 2, "1.1. Для КЛ-1 предусмотреть кабель 10 кВ.")
    child.section_path = "Раздел Б"
    atoms = atomize_fragments("doc", [first, child])
    assert len(atoms) == 2
    assert "parent" not in {source.fragment_id for source in atoms[-1].context_sources}
    assert "parent_scope_unresolved" in atoms[-1].warnings
    assert "context_scope_requires_review" in atoms[-1].warnings


def test_same_prose_section_parent_is_kept_with_manual_scope_warning():
    first = fragment("parent", 1, "1. Только при наличии КЛ-2.")
    second = fragment("child", 2, "1.1. Для КЛ-1 предусмотреть кабель 10 кВ.")
    between = fragment("heading", 1, "1.1. Требования к линиям")
    between.block_type = "heading"
    between.section_path = "Раздел 1 / Требования к линиям"
    second.section_path = "Раздел 1 / Требования к линиям"
    atoms = atomize_fragments("doc", [first, between, second])
    assert "parent" in {source.fragment_id for source in atoms[-1].context_sources}
    assert "context_scope_requires_review" in atoms[-1].warnings


def test_different_tables_in_same_section_never_exchange_parent_context():
    first = fragment("old", 1, "1. Только при наличии резервной КЛ-2.")
    first.block_type = "table_row"
    first.container_id = "docx-table-1"
    second = fragment("new", 2, "1.1. Для КЛ-1 предусмотреть кабель 10 кВ.")
    second.block_type = "table_row"
    second.container_id = "docx-table-2"

    atoms = atomize_fragments("doc", [first, second])
    assert len(atoms) == 2
    child = atoms[-1]
    assert child.sources[0].fragment_id == "new"
    assert "old" not in {source.fragment_id for source in child.context_sources}
    assert "parent_scope_unresolved" in child.warnings
    assert child.id == atomize_fragments("doc", [first, second])[-1].id


def test_legacy_table_rows_without_ids_are_never_automatically_merged():
    old = fragment("old", 1, "1. Только при наличии КЛ-2.")
    old.block_type = "table_row"
    new = fragment("new", 2, "1.1. Для КЛ-1 предусмотреть кабель 10 кВ.")
    new.block_type = "table_row"
    child = atomize_fragments("doc", [old, new])[-1]
    assert child.sources[0].fragment_id == "new"
    assert "old" not in {source.fragment_id for source in child.context_sources}
    assert "parent_scope_unresolved" in child.warnings


def test_distinct_table_comments_are_not_silently_discarded():
    first = fragment("old", 1, "1. Допускается кабель 95 мм².")
    first.block_type = "table_row"
    first.container_id = "docx-table-1"
    note = fragment("note", 2, "Только при наличии двух линий")
    note.block_type = "table_row"
    note.container_id = "docx-table-2"
    atoms = atomize_fragments("doc", [first, note])
    assert len(atoms) == 2
    assert "Только при наличии двух линий" == atoms[1].text
    assert [source.fragment_id for source in atoms[1].sources] == ["note"]


def test_docx_parser_assigns_distinct_ids_to_adjacent_tables():
    from io import BytesIO
    from docx import Document as WordDocument
    from app.documents.parser import _extract_docx

    word = WordDocument()
    word.add_heading("Раздел 1", level=1)
    old_table = word.add_table(rows=1, cols=1)
    old_table.cell(0, 0).text = "1. Только при наличии КЛ-2."
    next_table = word.add_table(rows=1, cols=1)
    next_table.cell(0, 0).text = "1.1. Для КЛ-1 предусмотреть кабель 10 кВ."
    output = BytesIO()
    word.save(output)

    blocks = _extract_docx(output.getvalue())
    rows = [b for b in blocks if b.block_type == "table_row"]
    assert [r.container_id for r in rows] == ["docx-table-1", "docx-table-2"]
    assert rows[0].section_path == rows[1].section_path

    fragments = [
        Fragment(
            id=f"f{index}", document_id="doc", page=b.page,
            position=b.position, text=b.text, block_type=b.block_type,
            section_path=b.section_path, container_id=b.container_id,
        )
        for index, b in enumerate(blocks)
    ]
    child = atomize_fragments("doc", fragments)[-1]
    assert "f1" not in {s.fragment_id for s in child.context_sources}
    assert "parent_scope_unresolved" in child.warnings


def test_known_table_cannot_inherit_prose_parent_without_scope_proof():
    parent = fragment("intro", 1, "1. Только при наличии КЛ-2.")
    child = fragment("table", 2, "1.1. Для КЛ-1 предусмотреть кабель 10 кВ.")
    child.block_type = "table_row"
    child.container_id = "docx-table-1"
    result = atomize_fragments("doc", [parent, child])
    assert len(result) == 2
    assert "intro" not in {x.fragment_id for x in result[-1].context_sources}
    assert "parent_scope_unresolved" in result[-1].warnings



def test_unresolved_parent_scope_prevents_automatic_support_even_with_identical_quotes():
    from app.requirements.schemas import TURequirement, ZNPClause, RequirementCategory
    from app.requirements.znp_first import ZnPFirstAgent

    parent = fragment("parent", 1, "1. Только при наличии резервной КЛ-2.")
    parent.section_path = "Раздел А"
    child = fragment("child", 2, "1.1. Для КЛ-1 предусмотреть кабель 10 кВ.")
    child.section_path = "Раздел Б"
    source = atomize_fragments("doc", [parent, child])[-1]

    requirement = TURequirement(
        id="tu1", clause_number="1.1", category=RequirementCategory.other,
        text=source.text, sources=[{
            "fragment_id": "tu1", "page": 1, "text": source.text,
        }],
    )
    atom = ZNPClause(
        id=source.id, clause_number=source.clause_number, text=source.text,
        sources=[{"fragment_id": s.fragment_id, "page": s.page, "text": s.text}
                 for s in source.sources],
        context_sources=[{"fragment_id": s.fragment_id, "page": s.page, "text": s.text}
                         for s in source.context_sources],
        warnings=list(source.warnings),
    )
    link = ZnPFirstAgent._validated_link(
        atom, requirement, {
            "relation": "supports",
            "tu_quote": source.text,
            "znp_quote": source.text,
            "confidence": 0.99,
            "reasoning": "identical",
        }, 0.95,
    )
    assert link is not None
    assert link["param_check"]["params_match"]
    assert link["scope_consistent"]
    assert link["relation"] == "partial"
    assert "parent_scope_unresolved" in link["warnings"]



def test_unnumbered_prose_note_after_table_remains_with_condition_and_warning():
    row = fragment("row", 1, "1. Допускается кабель 95 мм².")
    row.block_type = "table_row"
    row.container_id = "docx-table-1"
    note = fragment("note", 2, "Только при наличии двух независимых линий.")
    note.block_type = "paragraph"
    result = atomize_fragments("doc", [row, note])
    assert len(result) == 1
    assert "Только при наличии двух независимых линий." in result[0].text
    assert {source.fragment_id for source in result[0].sources} == {"row", "note"}
    assert "mixed_table_prose_scope_requires_review" in result[0].warnings
    assert "context_scope_requires_review" in result[0].warnings



def test_retrieval_chunks_do_not_join_adjacent_tables_with_same_section():
    from app.documents.chunking import build_chunks

    old = fragment("old", 1, "1. Ограничение только для КЛ-2.")
    new = fragment("new", 2, "1.1. Для КЛ-1 предусмотреть кабель 10 кВ.")
    for fragment_item in (old, new):
        fragment_item.block_type = "table_row"
    old.container_id = "docx-table-1"
    new.container_id = "docx-table-2"

    chunks = build_chunks([old, new])
    assert len(chunks) == 2
    assert [chunk.pieces[0].fragment.id for chunk in chunks] == ["old", "new"]


def test_legacy_table_rows_without_container_id_do_not_share_retrieval_chunk():
    from app.documents.chunking import build_chunks

    old = fragment("old", 1, "1. Условие для КЛ-2.")
    new = fragment("new", 2, "1.1. Условие для КЛ-1.")
    for fragment_item in (old, new):
        fragment_item.block_type = "table_row"
    chunks = build_chunks([old, new])
    assert len(chunks) == 2



def test_repeated_heading_text_does_not_reuse_prior_section_instance_parent():
    older_heading = fragment("h1", 1, "Раздел 1. Требования")
    newer_heading = fragment("h2", 3, "Раздел 1. Требования")
    for heading in (older_heading, newer_heading):
        heading.block_type = "heading"
        heading.section_path = "Раздел 1"
    parent = fragment("p1", 2, "1. Только при наличии резервной КЛ-2.")
    child = fragment("c2", 4, "1.1. Для КЛ-1 предусмотреть кабель 10 кВ.")

    result = atomize_fragments(
        "doc", [older_heading, parent, newer_heading, child],
    )
    assert len(result) == 2
    last = result[-1]
    context_ids = {source.fragment_id for source in last.context_sources}
    assert "h2" in context_ids
    assert "h1" not in context_ids
    assert "p1" not in context_ids
    assert "parent_scope_unresolved" in last.warnings


def test_prose_note_cannot_bridge_two_distinct_tables():
    old = fragment("old-table", 1, "1. Допускается кабель 95 мм².")
    old.block_type = "table_row"
    old.container_id = "docx-table-1"
    note = fragment("note-between", 2, "Только при наличии двух линий.")
    new = fragment("new-table", 3, "Мощность | 236 МВт")
    new.block_type = "table_row"
    new.container_id = "docx-table-2"
    atoms = atomize_fragments("doc", [old, note, new])
    assert len(atoms) == 2
    assert {s.fragment_id for s in atoms[0].sources} == {"old-table", "note-between"}
    assert [s.fragment_id for s in atoms[1].sources] == ["new-table"]
    assert "Только при наличии двух линий." in atoms[0].text
