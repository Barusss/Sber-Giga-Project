"""Focused regressions for original quotes, editions and registry updates."""
from types import SimpleNamespace

from app.normatives.extraction import (
    REFERENCE_PATTERN, enrich_candidate_code, normalize_code, own_standard_code,
)


def test_enrichment_does_not_invent_editions():
    assert enrich_candidate_code("ГОСТ 29322", "Использован ГОСТ 29322 в проекте") == "ГОСТ 29322"
    assert enrich_candidate_code("ГОСТ 1.0", "См. ГОСТ 1.0 в нормативных ссылках") == "ГОСТ 1.0"
    assert enrich_candidate_code("ГОСТ 29322—92", "ГОСТ 29322—92") == "ГОСТ 29322-92"


def test_enrichment_requires_explicit_edition_suffix():
    assert enrich_candidate_code("ГОСТ 29322", "См. ГОСТ 29322 - 92 в документе") == "ГОСТ 29322-92"
    assert enrich_candidate_code("ГОСТ 29322", "ГОСТ 29322 и 2014 документ") == "ГОСТ 29322"


def test_pdf_soft_hyphens_keep_the_edition():
    # PyMuPDF and embedded fonts commonly emit U+00AD between digits.
    raw = "ГОСТ 29322\u00ad92"
    assert REFERENCE_PATTERN.search(raw).group(0) == raw
    assert normalize_code(raw) == "ГОСТ 29322-92"
    assert enrich_candidate_code("ГОСТ 29322", "См. ГОСТ 29322\u00ad92") == "ГОСТ 29322-92"

    iec = "IEC 61000\u00ad4\u00ad30:2008"
    assert REFERENCE_PATTERN.search(iec).group(0) == iec
    assert normalize_code(iec) == "IEC 61000-4-30:2008"


def test_pdf_horizontal_bar_keeps_the_edition():
    # Some embedded PDF fonts emit U+2015 HORIZONTAL BAR instead of a dash.
    raw = "ГОСТ 30804.4.30\u20152013"
    assert REFERENCE_PATTERN.search(raw).group(0) == raw
    assert normalize_code(raw) == "ГОСТ 30804.4.30-2013"
    assert enrich_candidate_code(
        "ГОСТ 30804.4.30", "См. " + raw
    ) == "ГОСТ 30804.4.30-2013"


def test_nbsp_section_headings_are_recognized():
    from app.normatives.extraction import SECTION_PATTERN, section_after

    heading = "2\u00a0Нормативные\u00a0ссылки"
    match = SECTION_PATTERN.search(heading)
    assert match is not None
    assert section_after("foreword", match.group("heading")) == "normative_references"


def test_inline_bibliography_heading_is_classified_as_bibliography():
    from app.normatives.extraction import find_candidates

    fragment = SimpleNamespace(
        id="fragment-1", page=1, position=0,
        text=("3 Термины и определения\n"
              "Библиография IEC 61000-4-30:2008 Flickermeter requirements."),
    )
    chunk = SimpleNamespace(sources=[SimpleNamespace(fragment=fragment, text=fragment.text)])
    candidates, _ = find_candidates([chunk])
    iec = next(item for item in candidates if item["code"].startswith("IEC"))
    assert iec["section"] == "bibliography"


def test_adjective_standartnye_is_not_a_cover_marker():
    document = SimpleNamespace(filename="document.docx")
    chunk = SimpleNamespace(page_start=1, text=(
        "2 Нормативные ссылки\n"
        "ГОСТ 29322-92 Стандартные напряжения\n"
        "Согласно ГОСТ 30804.4.30-2013 измеряют параметры."
    ))
    assert own_standard_code(document, [chunk]) is None


def test_patch_rejects_null_required_fields(client):
    normative = client.post("/api/v1/normatives", json={
        "code": "ГОСТ 12345-2020", "title": "Название", "status": "active",
    })
    assert normative.status_code == 201, normative.text
    url = f"/api/v1/normatives/{normative.json()['id']}"
    assert client.patch(url, json={"title": None}).status_code == 422
    assert client.patch(url, json={"status": None}).status_code == 422
    assert client.patch(url, json={"title": ""}).status_code == 422
    assert client.patch(url, json={"title": "Новое"}).status_code == 200
