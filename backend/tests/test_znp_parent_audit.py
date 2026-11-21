"""Offline source-level parent-scope audit; no DeepSeek or private text leaks."""
import json

import pytest
from docx import Document as WordDocument

from scripts.audit_znp_parent_scope import audit_znp, compare_audits


def _sample(tmp_path):
    document = WordDocument()
    document.add_heading("Раздел 1", level=1)
    first = document.add_table(rows=1, cols=1)
    first.cell(0, 0).text = "1. Только при наличии резервной КЛ-2. CONFIDENTIAL_MARKER"
    second = document.add_table(rows=1, cols=1)
    second.cell(0, 0).text = "1.1. Для КЛ-1 предусмотреть кабель 10 кВ."
    path = tmp_path / "source.docx"
    document.save(path)
    return path


def test_offline_audit_records_table_provenance_without_source_text(tmp_path):
    path = _sample(tmp_path)
    result = audit_znp(path)
    assert result["summary"]["tables_identified"] == 2
    assert result["summary"]["atoms"] == 2
    assert result["summary"]["scope_anomalies"] == 0
    child = result["atoms"][-1]
    assert "parent_scope_unresolved" in child["warnings"]
    assert child["parent_refs"] == []
    assert result == audit_znp(path)
    assert "CONFIDENTIAL_MARKER" not in json.dumps(result, ensure_ascii=False)
    assert "Только при наличии резервной" not in json.dumps(result, ensure_ascii=False)


def test_comparison_keeps_fragment_identity_and_flags_context_changes(tmp_path):
    path = _sample(tmp_path)
    after = audit_znp(path)
    before = json.loads(json.dumps(after))
    before["atoms"][1]["context_ids"] += before["atoms"][0]["source_ids"]
    before["atoms"][1]["warnings"] = []
    changed = compare_audits(before, after)
    assert changed["lost_source_fragment_ids"] == []
    assert changed["new_source_fragment_ids"] == []
    assert len(changed["changed_context_or_warnings"]) == 1
    assert changed["changed_context_or_warnings"][0]["old_context_ids"]


def test_comparison_rejects_different_document_bytes(tmp_path):
    path = _sample(tmp_path)
    result = audit_znp(path)
    other = json.loads(json.dumps(result))
    other["source_sha256"] = "another file"
    with pytest.raises(ValueError, match="different source files"):
        compare_audits(other, result)


def test_comparison_detects_different_clause_grouping(tmp_path):
    path = _sample(tmp_path)
    after = audit_znp(path)
    before = json.loads(json.dumps(after))
    first, second = before["atoms"]
    first["source_ids"] += second["source_ids"]
    first["source_key"] = "simulated-old-group"
    before["atoms"] = [first]
    diff = compare_audits(before, after)
    assert diff["regrouped_fragment_ids"]
    assert diff["lost_source_fragment_ids"] == []
