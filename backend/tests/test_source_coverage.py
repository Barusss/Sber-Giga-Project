"""Exhaustive ZnP source ledger and safe technical-annex regression tests.

All tests are offline. No DeepSeek, embeddings or document uploads.
"""
from __future__ import annotations

from dataclasses import asdict
import json

import pytest
from docx import Document as WordDocument

from app.core.models import Fragment
from app.documents.parser import extract_blocks
from app.requirements.atomization import AtomSource, DocumentAtom, atomize_fragments
from app.requirements.extraction import extract_clauses
from app.requirements.schemas import (
    CheckRequirementsResponse, RequirementCategory,
    RequirementCheckItem, RequirementStatus, TURequirement,
)
from app.requirements.source_coverage import (
    apply_source_coverage_guard, source_coverage_ledger,
    source_coverage_summary,
)
from scripts.audit_znp_source_coverage import audit_file, main


def fragment(name, index, text, *, block_type="paragraph", section_path="Раздел 1"):
    return Fragment(
        id=name, document_id="doc", page=1, position=index,
        text=text, block_type=block_type, section_path=section_path,
    )


def test_ledger_accounts_for_every_fragment_and_never_silently_excludes_technical():
    requirement = fragment("source", 1, "1.1. Установить защиту 10 кВ.")
    heading = fragment("context", 0, "Раздел 1. Оборудование", block_type="heading")
    toc = fragment("toc", 2, "1. Общие положения ........ 3", block_type="toc")
    missing = fragment("annex", 3, "Приложение: параметры РЗА 10 кВ.")
    atom = DocumentAtom(
        "atom", "doc", "1.1", requirement.text, "Раздел 1",
        (AtomSource("source", 1, 0, len(requirement.text), requirement.text),),
        (AtomSource("context", 1, 0, len(heading.text), heading.text),),
        (),
    )
    ledger = source_coverage_ledger([heading, requirement, toc, missing], [atom])
    assert ledger["total_fragments"] == 4
    assert ledger["by_role"] == {
        "context_only": 1, "atom_source": 1, "excluded": 1, "unclassified": 1,
    }
    assert ledger["unclassified_fragment_ids"] == ["annex"]
    assert ledger["unclassified_by_block_type"] == {"paragraph": 1}
    assert not ledger["complete"]
    assert "параметры РЗА" not in json.dumps(ledger, ensure_ascii=False)
    assert "records" not in source_coverage_summary(ledger)


def test_unknown_fragment_provenance_is_not_counted_as_complete():
    source = fragment("source", 0, "1.1. Предусмотреть защиту.")
    atom = DocumentAtom(
        "atom", "doc", "1.1", source.text, source.section_path,
        (AtomSource("not-in-document", 1, 0, 2, "XX"),), (), (),
    )
    with pytest.raises(ValueError, match="absent parser fragments"):
        source_coverage_ledger([source], [atom])


def test_unclassified_fragments_downgrade_only_automatic_fulfilled():
    requirement = TURequirement(
        id="t1", clause_number="1.1", category=RequirementCategory.other,
        text="Установить защиту 10 кВ.",
    )
    fulfilled = RequirementCheckItem(
        id="r1", requirement=requirement, status=RequirementStatus.fulfilled,
        automatic_status=RequirementStatus.fulfilled, confidence_score=.99,
        reasoning="Подтверждено.",
    )
    other = RequirementCheckItem(
        id="r2", requirement=requirement, status=RequirementStatus.needs_review,
        confidence_score=0.0, reasoning="Нет доказательств.",
    )
    response = CheckRequirementsResponse(
        checked_requirements=[fulfilled, other],
        stats={"fulfilled": 1, "needs_review": 1, "total": 2},
        trace_info={"degraded": False},
    )
    ledger = source_coverage_ledger(
        [fragment("missing", 0, "Напряжение 10 кВ.")], [],
    )
    apply_source_coverage_guard(response, ledger)
    assert response.stats["fulfilled"] == 0
    assert response.stats["needs_review"] == 2
    assert fulfilled.status == RequirementStatus.needs_review
    assert fulfilled.automatic_status == RequirementStatus.needs_review
    assert fulfilled.confidence_score == 0.0
    assert fulfilled.validation_details["source_coverage_incomplete"]
    assert response.trace_info["source_coverage"]["downgraded_fulfilled"] == 1
    assert response.trace_info["degraded"] is True
    assert other.reasoning == "Нет доказательств."


def test_complete_ledger_preserves_fulfilled_and_confidence():
    requirement = TURequirement(
        id="t", clause_number="1", category=RequirementCategory.other,
        text="Установить защиту.",
    )
    row = RequirementCheckItem(
        id="r", requirement=requirement, status=RequirementStatus.fulfilled,
        confidence_score=.91,
    )
    response = CheckRequirementsResponse(
        checked_requirements=[row], stats={"fulfilled": 1, "total": 1},
    )
    source = fragment("s", 1, "1. Установить защиту.")
    atom = atomize_fragments("doc", [source])[0]
    ledger = source_coverage_ledger([source], [atom])
    assert ledger["complete"]
    apply_source_coverage_guard(response, ledger)
    assert row.status == RequirementStatus.fulfilled
    assert row.confidence_score == .91
    assert response.stats["fulfilled"] == 1
    assert response.trace_info["source_coverage"]["complete"]


def test_technical_annex_is_extracted_and_carries_scope_warning():
    heading = fragment(
        "h", 0, "Приложение 1. Параметры защит",
        block_type="heading", section_path="Приложение 1. Параметры защит",
    )
    row = fragment(
        "row", 1, "1. Уставка защиты КЛ-1 — 10 кВ.",
        block_type="table_row", section_path=heading.section_path,
    )
    row.container_id = "docx-table-1"
    # Existing nonconservative TU behavior is unchanged.
    assert extract_clauses([heading, row], conservative=False) == []
    atoms = atomize_fragments("doc", [heading, row])
    assert len(atoms) == 1
    assert atoms[0].sources[0].fragment_id == "row"
    assert "appendix_scope_requires_review" in atoms[0].warnings
    ledger = source_coverage_ledger([heading, row], atoms)
    assert ledger["complete"]


def test_abbreviations_annex_stays_service_not_automatically_requirement():
    heading = fragment(
        "h", 0, "Приложение А. Перечень сокращений",
        block_type="heading", section_path="Приложение А. Перечень сокращений",
    )
    row = fragment(
        "row", 1, "КЛ — кабельная линия",
        block_type="table_row", section_path=heading.section_path,
    )
    row.container_id = "docx-table-1"
    assert extract_clauses([heading, row], conservative=True) == []
    ledger = source_coverage_ledger([heading, row], [])
    assert ledger["unclassified_count"] == 2
    assert not ledger["complete"]


def test_original_docx_annex_table_reaches_atoms_with_exact_sources(tmp_path):
    word = WordDocument()
    word.add_heading("Задание на проектирование", level=1)
    word.add_heading("Приложение 2. Параметры РЗА", level=1)
    t = word.add_table(rows=1, cols=1)
    t.cell(0, 0).text = "1. КЛ-1: напряжение 10 кВ."
    path = tmp_path / "source.docx"
    word.save(path)
    ledger, fragments = audit_file(path)
    assert ledger["total_fragments"] == len(fragments)
    assert ledger["atoms"] >= 1
    table = [fragment for fragment in fragments if fragment.block_type == "table_row"]
    assert len(table) == 1
    row_record = next(record for record in ledger["records"]
                      if record["fragment_id"] == table[0].id)
    assert row_record["role"] == "atom_source"


def test_cli_never_writes_raw_private_text_without_explicit_opt_in(tmp_path, capsys):
    word = WordDocument()
    word.add_heading("Приложение А. Перечень сокращений", level=1)
    table = word.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "Секретное приложение: КЛ-2, 120 мм²"
    path = tmp_path / "source.docx"
    word.save(path)
    out = tmp_path / "diagnostics" / "audit.json"
    assert main(["--znp", str(path), "--out", str(out)]) == 0
    assert "Секретное" not in out.read_text(encoding="utf-8")
    assert "Секретное" not in capsys.readouterr().out
    private = tmp_path / "private" / "unclassified.jsonl"
    assert main([
        "--znp", str(path), "--out", str(out),
        "--private-review", str(private),
    ]) == 0
    assert private.is_file()
    assert "Секретное" in private.read_text(encoding="utf-8")



@pytest.mark.parametrize("source_text,kind", [
    ("10", "table_row"),
    ("6", "paragraph"),
    ("А", "table_row"),
    ("0,4", "table_row"),
])
def test_short_numeric_and_table_cells_are_not_silently_excluded(source_text, kind):
    value = fragment("table-value", 0, source_text, block_type=kind)
    ledger = source_coverage_ledger([value], [])
    assert ledger["by_role"].get("excluded", 0) == 0
    assert ledger["unclassified_fragment_ids"] == ["table-value"]



def test_source_ledger_ids_match_prior_parent_scope_audit(tmp_path):
    """Exact old audit ID scheme lets Codex reconcile all original fragments."""
    from scripts.audit_znp_parent_scope import audit_znp

    word = WordDocument()
    word.add_heading("Раздел 1", level=1)
    word.add_paragraph("1.1. Для КЛ-1 предусмотреть 10 кВ.")
    path = tmp_path / "same.docx"
    word.save(path)
    ledger, _ = audit_file(path)
    previous = audit_znp(path)
    assert ledger["source_sha256"] == previous["source_sha256"]
    assert [row["fragment_id"] for row in ledger["records"]] == [
        row["id"] for row in previous["fragments"]
    ]
    assert [row["text_sha256"] for row in ledger["records"]] == [
        row["text_hash"] for row in previous["fragments"]
    ]



def test_number_sign_appendix_requires_manual_scope_review():
    heading = fragment(
        "h", 0, "Приложение № 2. Настройки РЗА",
        block_type="heading", section_path="Приложение № 2. Настройки РЗА",
    )
    row = fragment(
        "r", 1, "1. Предусмотреть уставку 10 кВ.",
        block_type="table_row", section_path=heading.section_path,
    )
    row.container_id = "docx-table-1"
    atoms = atomize_fragments("doc", [heading, row])
    assert len(atoms) == 1
    assert "appendix_scope_requires_review" in atoms[0].warnings


def test_normative_catalog_does_not_drop_unnumbered_applicability_rule():
    section = "Приложение А. Перечень нормативных документов"
    heading = fragment("catalog", 0, section, block_type="heading", section_path=section)
    rule = fragment(
        "rule", 1,
        "В настоящем приложении приведены нормативные документы. "
        "При их изменении проектировщик должен согласовать применяемую редакцию.",
        section_path=section,
    )
    reference = fragment("reference", 2, "100. ГОСТ Р 12345-2024. Требования к защите.", section_path=section)
    atoms = atomize_fragments("doc", [heading, rule, reference])
    assert any(s.fragment_id == "rule" for a in atoms for s in a.sources)
    assert any("appendix_scope_requires_review" in a.warnings for a in atoms)
    ledger = source_coverage_ledger([heading, rule, reference], atoms)
    assert next(r for r in ledger["records"] if r["fragment_id"] == "rule")["role"] == "atom_source"
    assert next(r for r in ledger["records"] if r["fragment_id"] == "reference")["role"] == "unclassified"


def test_large_normative_reference_catalog_does_not_fake_complete_coverage():
    """A long reference list is not 200 independent automatically met requirements.

    Yet its entries must remain traceable/unclassified until the applicability
    of their editions has been checked against the source document.
    """
    section = "Приложение А. Перечень нормативных документов"
    heading = fragment("catalog-title", 0, section, block_type="heading",
                       section_path=section)
    rule = fragment(
        "edition-rule", 1,
        "В настоящем приложении приведены нормативные документы. "
        "При их изменении проектировщик должен согласовать применяемую редакцию.",
        section_path=section,
    )
    references = [
        fragment(
            f"reference-{index}", index + 2,
            f"{index}. ГОСТ Р 12345-{2020 + (index % 5)}. "
            "Справочный перечень нормативных документов.",
            section_path=section,
        )
        for index in range(1, 201)
    ]
    fragments = [heading, rule, *references]
    atoms = atomize_fragments("doc", fragments)
    ledger = source_coverage_ledger(fragments, atoms)
    referenced = {s.fragment_id for atom in atoms for s in atom.sources}
    assert "edition-rule" in referenced
    assert not any(ref.id in referenced for ref in references)
    assert ledger["unclassified_count"] == 200
    assert not ledger["complete"]
    assert {row["fragment_id"] for row in ledger["records"]
            if row["role"] == "unclassified"} == {ref.id for ref in references}
    assert all(row["text_sha256"] for row in ledger["records"])


def test_styled_headings_in_word_contents_are_not_duplicate_requirements(tmp_path):
    """An explicit TOC may contain Heading-styled entries without page numbers."""
    from app.documents.parser import _extract_docx

    word = WordDocument()
    word.add_paragraph("Содержание")
    word.add_paragraph("1. ОБЩИЕ ПОЛОЖЕНИЯ", style="Heading 1")
    word.add_paragraph("1.1. Общая схема электроснабжения", style="Heading 2")
    word.add_paragraph("1. ОБЩИЕ ПОЛОЖЕНИЯ", style="Heading 1")
    word.add_paragraph("1.1. Общая схема электроснабжения", style="Heading 2")
    word.add_paragraph("1.1. Предусмотреть напряжение 10 кВ.")
    from io import BytesIO
    buf = BytesIO()
    word.save(buf)

    blocks = _extract_docx(buf.getvalue())
    assert [(x.text, x.block_type) for x in blocks[:3]] == [
        ("Содержание", "toc"),
        ("1. ОБЩИЕ ПОЛОЖЕНИЯ", "toc"),
        ("1.1. Общая схема электроснабжения", "toc"),
    ]
    assert [x.block_type for x in blocks[3:]] == [
        "heading", "heading", "paragraph",
    ]
    assert blocks[-1].section_path == (
        "1. ОБЩИЕ ПОЛОЖЕНИЯ / 1.1. Общая схема электроснабжения"
    )
    from app.documents.parser import extract_blocks
    parsed = extract_blocks(buf.getvalue(), ".docx")
    assert len(parsed) == len(blocks)


def test_toc_heuristic_does_not_hide_unique_technical_heading(tmp_path):
    """A unique heading after contents is not disposable without evidence."""
    from app.documents.parser import _extract_docx
    from io import BytesIO

    word = WordDocument()
    word.add_paragraph("Содержание")
    word.add_paragraph("1. ТЕХНИЧЕСКИЕ ТРЕБОВАНИЯ", style="Heading 1")
    word.add_paragraph("1.1. Предусмотреть резервную кабельную линию 10 кВ.")
    buf = BytesIO()
    word.save(buf)
    blocks = _extract_docx(buf.getvalue())
    assert [block.block_type for block in blocks] == [
        "toc", "heading", "paragraph",
    ]
    assert blocks[-1].section_path == "1. ТЕХНИЧЕСКИЕ ТРЕБОВАНИЯ"


def test_explicit_word_toc_style_is_provenance_even_without_page_leaders(tmp_path):
    from app.documents.parser import _extract_docx
    from docx.enum.style import WD_STYLE_TYPE
    from io import BytesIO

    word = WordDocument()
    word.styles.add_style("TOC 1", WD_STYLE_TYPE.PARAGRAPH)
    word.add_paragraph("1. Общие положения", style="TOC 1")
    word.add_paragraph("1. Общие положения", style="Heading 1")
    word.add_paragraph("1.1. Установить защиту 10 кВ.")
    buf = BytesIO()
    word.save(buf)
    blocks = _extract_docx(buf.getvalue())
    assert [block.block_type for block in blocks] == ["toc", "heading", "paragraph"]
    assert blocks[-1].section_path == "1. Общие положения"


def test_normative_reference_triage_hint_never_marks_source_complete():
    section = "Приложение А. Перечень нормативных документов"
    reference = fragment(
        "ref", 1, "100. ГОСТ 32109-2024. Требования к оборудованию.",
        section_path=section,
    )
    technical_rule = fragment(
        "rule", 2,
        "При изменении редакции ГОСТ 32109 требуется согласование.",
        section_path=section,
    )
    noncatalog = fragment(
        "requirement", 3,
        "100. ГОСТ 32109-2024. Установить защиту 10 кВ.",
        section_path="Раздел 1. Технические требования",
    )
    ledger = source_coverage_ledger(
        [reference, technical_rule, noncatalog], [],
    )
    entries = {row["fragment_id"]: row for row in ledger["records"]}
    assert all(row["role"] == "unclassified" for row in entries.values())
    assert entries["ref"]["review_hint"] == "normative_reference_candidate"
    assert entries["rule"]["review_hint"] is None
    assert entries["requirement"]["review_hint"] is None
    assert ledger["review_hint_counts"] == {
        "normative_reference_candidate": 1
    }
    assert ledger["unclassified_count"] == 3
    assert not ledger["complete"]


def test_normative_catalog_hint_does_not_change_source_guard_behavior():
    section = "Приложение 3. Перечень нормативных документов"
    fragment_value = fragment(
        "ref", 1,
        "12. СНиП 3.05.06-85. Электротехнические устройства.",
        section_path=section,
    )
    ledger = source_coverage_ledger([fragment_value], [])
    assert ledger["review_hint_counts"]["normative_reference_candidate"] == 1
    assert source_coverage_summary(ledger)["complete"] is False
    assert ledger["by_role"]["unclassified"] == 1



def test_pdf_image_with_native_header_still_uses_ocr(monkeypatch):
    """Public PDF pattern: searchable colontitle over scanned requirement image."""
    from io import BytesIO

    import fitz
    from PIL import Image

    from app.documents.parser import ExtractedBlock, extract_blocks

    pdf = fitz.open()
    page = pdf.new_page(width=600, height=800)
    picture = Image.new("RGB", (600, 800), color="white")
    stream = BytesIO()
    picture.save(stream, "PNG")
    page.insert_image(page.rect, stream=stream.getvalue())
    page.insert_text((40, 45), "Project report header with page number 107")
    assert len(page.get_text()) > 30
    visited_pages = []

    def fake_page_ocr(page, image, page_idx, sections, toc_mode):
        visited_pages.append(page_idx)
        return [
            ExtractedBlock(
                page_idx, 0,
                "1. Для КЛ предусмотреть защиту 10 кВ.",
                "paragraph", None, None, "ocr",
            ),
        ], toc_mode

    monkeypatch.setattr("app.documents.parser._ocr_page_blocks", fake_page_ocr)
    result = extract_blocks(pdf.tobytes(), ".pdf")
    pdf.close()
    assert visited_pages == [1]
    assert len(result) == 1
    assert result[0].extraction_method == "ocr"
    assert "10 кВ" in result[0].text


def test_pdf_native_text_without_scan_does_not_use_ocr(monkeypatch):
    import fitz
    from app.documents.parser import extract_blocks

    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text(
        (40, 45),
        "1. Install cable with voltage 10 kV, do not change any limits.",
    )

    def forbidden_ocr(*args, **kwargs):
        raise AssertionError("No OCR should run for a text-only PDF")

    monkeypatch.setattr("app.documents.parser._ocr_page_blocks", forbidden_ocr)
    result = extract_blocks(pdf.tobytes(), ".pdf")
    pdf.close()
    assert result
    assert all(block.extraction_method == "text" for block in result)

def test_docx_contents_with_case_changes_and_a_nonmatching_entry(tmp_path):
    from io import BytesIO
    from app.documents.parser import extract_blocks

    word = WordDocument()
    for text in (
        "Содержание", "1. Исходные данные", "2. Неоднозначное название",
        "3. Ограничения", "1. ИСХОДНЫЕ ДАННЫЕ",
    ):
        word.add_paragraph(text, style="Heading 1")
    word.add_paragraph("1.1. Установить защиту.")
    word.add_heading("3. ОГРАНИЧЕНИЯ", level=1)
    word.add_paragraph("3.1. Предусмотреть резерв.")
    buf = BytesIO()
    word.save(buf)
    blocks = extract_blocks(buf.getvalue(), ".docx")
    assert [b.block_type for b in blocks] == [
        "toc", "toc", "heading", "toc", "heading", "paragraph", "heading", "paragraph",
    ]
    assert blocks[-1].section_path == "3. ОГРАНИЧЕНИЯ"

def test_unstructured_multipage_preamble_cannot_certify_two_letters():
    first = fragment("first", 0, "Директору АО. Необходимо обеспечить резервирование линии. Главный инженер И. И.", section_path=None)
    second = fragment("second", 0, "Директору АО. Согласовываем изменение схемы подключения. Главный инженер П. П.", section_path=None)
    second.page = 2
    sources = [first, second]
    assert extract_clauses(sources) == []
    atoms = atomize_fragments("doc", sources)
    assert not atoms
    ledger = source_coverage_ledger(sources, atoms)
    assert not ledger["complete"]
    assert set(ledger["unclassified_fragment_ids"]) == {"first", "second"}

def test_pdf_approval_does_not_swallow_numbered_design_rows():
    texts = ["Утверждаю:\nДиректор", "1. Заказчик\nПроектная организация",
             "10.Особые условия", "Сейсмичность района 9 баллов."]
    fragments = [fragment(str(i), i, t, section_path=None) for i, t in enumerate(texts)]
    for item in fragments:
        item.bbox = [1, 2, 10, 20]
    atoms = atomize_fragments("doc", fragments)
    assert [(a.clause_number, [s.fragment_id for s in a.sources]) for a in atoms] == [
        ("1", ["1"]), ("10", ["2", "3"]),
    ]

@pytest.mark.parametrize("text", [
    "1. Расчётная температура жидкости 48 °С,",
    "3. Насос циркуляционный — 2 шт, (тип уточнить)",
])
def test_ocr_numbered_design_values_are_source_text(text):
    from app.documents.parser import _ocr_heading_level

    assert _ocr_heading_level(text) is None

def test_ocr_detached_number_column_requires_aligned_text():
    from app.documents.parser import _detached_numbers

    labels = [(f"{n}.", [10, n * 12, 18, n * 12 + 8], (1, 1)) for n in (1, 2, 3)]
    bodies = [("Условие проектирования", [25, n * 12, 170, n * 12 + 9], (2, n))
              for n in (1, 2, 3)]
    assert set(_detached_numbers(labels + bodies)) == {"1", "2", "3"}
    assert not _detached_numbers(labels)

@pytest.mark.parametrize("alternate_y_delta, expected_calls_accepted", [(0, True), (10, False)])
def test_tesseract_rejoins_only_verified_numbered_rows(monkeypatch, alternate_y_delta, expected_calls_accepted):
    from types import SimpleNamespace
    import pytesseract
    from app.documents.parser import _ocr_lines
    from app.core.config import settings

    monkeypatch.setattr(settings, "ocr_backend", "tesseract")
    calls = []

    def ocr_data(image, *, config, **kwargs):
        mode = int(config.split()[-1])
        calls.append(mode)
        words = []
        for n in (1, 2, 3):
            words.append((f"{n}.", 10, n * 16, n))
        for n in (1, 2, 3):
            words.append(("Условие проектирования", 25, n * 16, n + 3))
        if mode == 4:
            words = [(f"{n}. | Условие проектирования", 10, n * 16 + alternate_y_delta, n)
                     for n in (1, 2, 3)]
        return dict(text=[w[0] for w in words], left=[w[1] for w in words],
                    top=[w[2] for w in words], block_num=[w[3] for w in words],
                    width=[8] * len(words), height=[8] * len(words),
                    par_num=[1] * len(words), line_num=[1] * len(words))

    monkeypatch.setattr(pytesseract, "image_to_data", ocr_data)
    size = SimpleNamespace(width=200, height=200)
    lines = _ocr_lines(SimpleNamespace(rect=size), size, 1)
    assert calls == [3, 4]
    assert (len(lines) == 3) == expected_calls_accepted

def test_pdf_signature_block_does_not_extend_last_requirement():
    texts = ["1. Предоставить комплект документов.", "ЗАКАЗЧИК ПОДРЯДЧИК", "Адрес организации"]
    fragments = [fragment(str(i), i, t, section_path=None) for i, t in enumerate(texts)]
    for item in fragments:
        item.bbox = [1, 2, 10, 20]
    atoms = atomize_fragments("doc", fragments)
    assert [s.fragment_id for s in atoms[0].sources] == ["0"]


def test_ocr_numbered_preservation_instruction_is_body():
    from app.documents.parser import _ocr_heading_level
    assert _ocr_heading_level("3.2. Кабельные вводы сохранить,") is None

def test_scanned_grid_has_no_automatic_cell_provenance(monkeypatch):
    import fitz
    from PIL import Image, ImageDraw
    from io import BytesIO
    from app.documents.parser import ExtractedBlock

    pdf = fitz.open()
    page = pdf.new_page(width=400, height=200)
    image = Image.new("RGB", (400, 200), "white")
    draw = ImageDraw.Draw(image)
    for y in (10, 50, 90, 130, 170):
        draw.line((10, y, 390, y), fill="black")
    draw.line((180, 50, 180, 170), fill="black")
    for xy, text in [((20, 20), "Design values"), ((20, 60), "1. Station"),
                     ((190, 60), "7 MW"), ((20, 100), "1. Transformer"),
                     ((190, 100), "9 MVA"), ((20, 140), "Reserve"), ((190, 140), "Not required")]:
        draw.text(xy, text, fill="black")
    stream = BytesIO()
    image.save(stream, "PNG")
    page.insert_image(page.rect, stream=stream.getvalue())
    # OCR deliberately returns perfectly legible text with an incorrect scope.
    def fake_ocr(page, image, page_idx, sections, toc_mode):
        return [ExtractedBlock(1, 0, "1. Station 9 MVA.", bbox=[10, 50, 380, 120],
                               extraction_method="ocr")], False
    monkeypatch.setattr("app.documents.parser._ocr_page_blocks", fake_ocr)
    blocks = extract_blocks(pdf.tobytes(), ".pdf")
    assert blocks[0].container_id == "table_structure_unverified:1"

def test_native_table_and_plain_text_keep_distinct_layout_status():
    import fitz
    from app.documents.parser import pdf_layout_version

    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((40, 50), "1. Nominal value of the test equipment.")
    assert pdf_layout_version("text-v3", extract_blocks(pdf.tobytes(), ".pdf")) == "text-v3+flow1"
    page.draw_line((170, 20), (170, 100))
    page.insert_text((190, 50), "7 MVA")
    page.insert_text((40, 80), "1. Operating temperature")
    page.insert_text((190, 80), "84 C")
    blocks = extract_blocks(pdf.tobytes(), ".pdf")
    assert all(b.container_id == "table_structure_unverified:1" for b in blocks)
    assert pdf_layout_version("text-v3", blocks) == "text-v3+review1"


@pytest.mark.parametrize("side", ["tu", "znp"])
def test_exact_pdf_quote_cannot_certify_cell_affiliation(side):
    from app.requirements.znp_first import ZnPFirstAgent
    from app.requirements.schemas import ZNPClause
    text = "Установить трансформатор мощностью 7 МВА."
    source = {"text": text, "fragment_id": "s", "page": 1}
    znp = ZNPClause(id="z", clause_number="1", text=text, sources=[source])
    tu = TURequirement(id="t", clause_number="1", category=RequirementCategory.other, text=text, sources=[source])
    proposal = dict(relation="supports", tu_quote=text, znp_quote=text, confidence=1)
    assert ZnPFirstAgent._validated_link(znp, tu, proposal, 1)["relation"] == "supports"
    (tu if side == "tu" else znp).warnings.append("table_structure_unverified")
    link = ZnPFirstAgent._validated_link(znp, tu, proposal, 1)
    assert link["relation"] == "partial" and "table_structure_unverified" in link["warnings"]

def test_partial_atom_source_never_marks_fragment_fully_covered():
    original = fragment("mixed", 1, "1. Местоположение. 9. Мощность 8,6 МВт.")
    text = "1. Местоположение."
    part = AtomSource(original.id, original.page, 0, len(text), text)
    atom = DocumentAtom("a", "doc", "1", text, None, (part,), (), ())
    ledger = source_coverage_ledger([original], [atom])
    assert ledger["unclassified_count"] == 1
    assert ledger["complete"] is False


def test_pdf_rows_atomize_values_with_the_correct_source_ranges():
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=600, height=400)
    for y in (80, 130, 180, 230):
        page.draw_line((40, y), (560, y))
    for x in (40, 280, 560):
        page.draw_line((x, 80), (x, 230))
    for y, label, value in (
        (110, "7. Location", "River"),
        (160, "9. Power", "8.6 MW cos 0.8"),
        (210, "10. Seismic", "9 points"),
    ):
        page.insert_text((55, y), label)
        page.insert_text((295, y), value)
    data = doc.tobytes()
    blocks = extract_blocks(data, ".pdf")
    fragments = [Fragment(id=str(i), document_id="doc", **vars(block))
                 for i, block in enumerate(blocks)]
    atoms = atomize_fragments("doc", fragments, pdf_data=data)
    rows = {a.clause_number: a for a in atoms}
    assert "8.6" not in rows["7"].text
    assert "8.6" in rows["9"].text and "0.8" in rows["9"].text
    assert "points" in rows["10"].text and "8.6" not in rows["10"].text
    original = {item.id: item for item in fragments}
    for atom in rows.values():
        assert atom.text == "\n".join(source.text for source in atom.sources)
        for source in atom.sources:
            assert original[source.fragment_id].text[source.start_offset:source.end_offset] == source.text

def test_pdf_repeated_values_keep_distinct_offsets():
    import fitz
    from app.requirements.pdf_cell_sources import bind_pdf_cells

    document = fitz.open()
    page = document.new_page(width=500, height=300)
    for y in (50, 100, 150):
        page.draw_line((40, y), (460, y))
    for x in (40, 240, 460):
        page.draw_line((x, 50), (x, 150))
    page.insert_text((50, 80), "1. First operating parameter")
    page.insert_text((250, 80), "9")
    page.insert_text((50, 130), "2. Second operating parameter")
    page.insert_text((250, 130), "9")
    data = document.tobytes()
    fragments = [Fragment(id=str(i), document_id="doc", **vars(block))
                 for i, block in enumerate(extract_blocks(data, ".pdf"))]
    cells = bind_pdf_cells(data, fragments)
    values = [(sources[0].fragment_id, sources[0].start_offset) for _, cell, sources, reason in cells
              if cell.column == 1 and reason is None]
    assert len(values) == 2 and len(set(values)) == 2


def test_partial_coverage_rejects_invalid_source_offsets():
    original = fragment("bad", 1, "Source text.")
    bad = AtomSource("bad", 1, 0, 6, "wrong")
    atom = DocumentAtom("a", "doc", "1", "wrong", None, (bad,), (), ())
    with pytest.raises(ValueError, match="Invalid source span"):
        source_coverage_ledger([original], [atom])

def test_pdf_cell_citation_cannot_use_neighbouring_source():
    from app.requirements.znp_first import ZnPFirstAgent
    from app.requirements.schemas import ZNPClause

    tu = TURequirement(id="tu", clause_number="1", category=RequirementCategory.other,
                       text="Power 8.6 MW", sources=[{"text": "Power 8.6 MW"}])
    znp = ZNPClause(id="z", clause_number="7", text="Location",
                    sources=[{"fragment_id": "same", "text": "Location",
                              "start_offset": 0, "end_offset": 8}],
                    fragment_text="Location Power 8.6 MW")
    proposal = dict(relation="supports", tu_quote="Power 8.6 MW",
                    znp_quote="Power 8.6 MW", confidence=1)
    assert ZnPFirstAgent._validated_link(znp, tu, proposal, 1) is None

def test_pdf_verified_rows_can_support_but_unverified_tu_cannot():
    import fitz
    from app.requirements.znp_first import ZnPFirstAgent
    from app.requirements.schemas import ZNPClause
    doc = fitz.open()
    page = doc.new_page(width=600, height=300)
    for y in (50, 100, 150):
        page.draw_line((40, y), (560, y))
    for x in (40, 280, 560):
        page.draw_line((x, 50), (x, 150))
    for y, number in ((80, 1), (130, 2)):
        page.insert_text((50, y), f"{number}. Transformer power")
        page.insert_text((300, y), "10 MVA transformer")
    data = doc.tobytes()
    fragments = [Fragment(id=str(i), document_id="doc", **vars(b))
                 for i, b in enumerate(extract_blocks(data, ".pdf"))]
    atoms = atomize_fragments("doc", fragments, pdf_data=data, unverified_pdf=True)
    assert len(atoms) == 2 and all(not a.warnings for a in atoms)
    a = atoms[0]
    znp = ZNPClause(id=a.id, clause_number=a.clause_number, text=a.text,
                    sources=[asdict(s) for s in a.sources], warnings=list(a.warnings))
    tu = TURequirement(id="tu", clause_number="1", category=RequirementCategory.other,
                        text=a.text, sources=znp.sources)
    quote = a.sources[-1].text
    proposal = dict(relation="supports", confidence=1, tu_quote=quote, znp_quote=quote)
    assert ZnPFirstAgent._validated_link(znp, tu, proposal, 1)["relation"] == "supports"
    tu.warnings = ["table_structure_unverified"]
    assert ZnPFirstAgent._validated_link(znp, tu, proposal, 1)["relation"] == "partial"
