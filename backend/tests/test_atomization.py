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
    heading.section_path = heading.text
    body = fragment("b", 3, "1.1.1. Кабель 95 мм².")
    body.section_path = heading.text
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
