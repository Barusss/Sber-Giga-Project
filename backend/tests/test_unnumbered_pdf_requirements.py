"""A technical PDF statement after approvals must not disappear with the title."""
from pathlib import Path

import pytest

from app.core.models import Fragment
from app.requirements.atomization import atomize_fragments
from app.requirements.extraction import extract_clauses


def fragment(index, text):
    return Fragment(id=f"f{index}", document_id="tu", page=1, position=index,
                    text=text, block_type="paragraph", bbox=[0, index * 10, 500, index * 10 + 9])


def test_technical_statement_after_title_is_separate_and_grounded():
    fragments = [fragment(0, "Технические условия"),
                 fragment(1, "На присоединение ГЭС мощностью 8,6 МВт"),
                 fragment(2, "Согласованная величина мощности электростанции — 8.6 МВт."),
                 fragment(3, "Подпись главного инженера"),
                 fragment(4, "1.1. Предусмотреть трансформатор мощностью 10 МВА.")]
    clauses = extract_clauses(fragments, conservative=True)
    recovered = next(c for c in clauses if "8.6" in c.text)
    assert recovered.fragment_ids == ["f2"]
    assert "10 МВА" not in recovered.text and "На присоединение" not in recovered.text
    atoms = atomize_fragments("tu", fragments)
    recovered = next(a for a in atoms if "8.6" in a.text)
    assert "unnumbered_technical_scope_requires_review" in recovered.warnings
    assert len(recovered.sources) == 1
    source = recovered.sources[0]
    assert fragments[2].text[source.start_offset:source.end_offset] == source.text
    assert not any(s.fragment_id in {"f1", "f3"} for a in atoms for s in a.sources)


def test_title_signatures_and_normative_catalog_are_not_recovered():
    fragments = [fragment(0, "Технические условия"),
                 fragment(1, "На присоединение малой ГЭС мощностью 8.6 МВт"),
                 fragment(2, '- «Схема выдачи мощности по проекту ГЭС мощностью 8,6 МВт»;'),
                 fragment(3, "ГОСТ Р 12345-2020. Оборудование 110 кВ."),
                 fragment(4, "Подпись главного инженера")]
    assert extract_clauses(fragments, conservative=True) == []


def test_original_koktal_tu_recovers_actual_station_parameter():
    from scripts.diagnose_deepseek_pair import load_fragments

    path = Path("/inputs/koktal_tu.pdf")
    if not path.is_file():
        pytest.skip("Private original Koktal TU unavailable")
    document_id, fragments = load_fragments(path, "tu")
    atoms = atomize_fragments(document_id, fragments, pdf_data=path.read_bytes(), unverified_pdf=True)
    recovered = [a for a in atoms if "Согласованная величина мощности" in a.text]
    assert len(recovered) == 1
    assert "8.6 МВт" in recovered[0].text
    assert "10 МВА" not in recovered[0].text
    assert "unnumbered_technical_scope_requires_review" in recovered[0].warnings
    by_id = {f.id: f for f in fragments}
    for source in recovered[0].sources:
        assert by_id[source.fragment_id].document_id == document_id
        assert by_id[source.fragment_id].text[source.start_offset:source.end_offset] == source.text
    # The frozen pool is evidence of the previous nine source identities.
    import json
    pool = Path("/large/pools.private.json")
    if pool.is_file():
        old = json.loads(pool.read_text())["pdf"]["tu"]
        assert {t["id"] for t in old} <= {a.id for a in atoms}
