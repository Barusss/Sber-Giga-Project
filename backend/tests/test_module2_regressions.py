"""Focused regressions for original quotes and edition restoration."""
from app.normatives.extraction import enrich_candidate_code


def test_enrichment_does_not_invent_editions():
    assert enrich_candidate_code("ГОСТ 29322", "Использован ГОСТ 29322 в проекте") == "ГОСТ 29322"
    assert enrich_candidate_code("ГОСТ 1.0", "См. ГОСТ 1.0 в нормативных ссылках") == "ГОСТ 1.0"
    assert enrich_candidate_code("ГОСТ 29322—92", "ГОСТ 29322—92") == "ГОСТ 29322-92"


def test_enrichment_requires_explicit_edition_suffix():
    assert enrich_candidate_code("ГОСТ 29322", "См. ГОСТ 29322 - 92 в документе") == "ГОСТ 29322-92"
    assert enrich_candidate_code("ГОСТ 29322", "ГОСТ 29322 и 2014 документ") == "ГОСТ 29322"
