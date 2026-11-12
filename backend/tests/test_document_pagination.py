from datetime import datetime, timezone

from app.core.schemas import DocumentOut


def test_docx_has_logical_pagination_and_pdf_has_physical_pagination():
    common = {"id": "d", "project_id": "p", "kind": "tu", "extraction_version": "v1", "created_at": datetime.now(timezone.utc)}
    docx = DocumentOut(**common, filename="test.docx", mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    pdf = DocumentOut(**common, filename="test.pdf", mime_type="application/pdf")
    assert docx.model_dump()["pagination_kind"] == "logical"
    assert pdf.model_dump()["pagination_kind"] == "physical"


def test_document_response_exposes_completed_normative_extraction():
    document = DocumentOut(id="d", project_id="p", kind="tu", filename="test.pdf",
        mime_type="application/pdf", extraction_version="v1", created_at=datetime.now(timezone.utc),
        normative_extraction_completed=True)
    assert document.model_dump()["normative_extraction_completed"] is True


def test_docx_filename_is_not_treated_as_pdf_when_mime_is_wrong():
    document = DocumentOut(id="d", project_id="p", kind="tu", filename="test.docx",
        mime_type="application/pdf", extraction_version="v1", created_at=datetime.now(timezone.utc))
    assert document.pagination_kind != "physical"
