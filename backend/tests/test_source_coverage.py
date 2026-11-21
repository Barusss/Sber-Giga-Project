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
    word.add_paragraph("Секретное приложение: КЛ-2, 120 мм²")
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
