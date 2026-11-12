"""Optional pgvector integration exercised by the dedicated CI postgres job."""
import json
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.models import Chunk, Document, DocumentKind, Project, TURequirement, RequirementVerification, RequirementCategory, RequirementStatus
from app.search.router import lexical_ids, semantic_ids


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="PostgreSQL job only")
def test_pgvector_and_russian_full_text():
    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    with Session(engine) as db:
        project = Project(name="vector index integration")
        db.add(project)
        db.flush()
        doc = Document(project_id=project.id, kind=DocumentKind.tu,
                       filename="test.pdf", stored_path="none", mime_type="application/pdf")
        db.add(doc)
        db.flush()
        v1 = [1.0] + [0.0] * 383
        v2 = [0.0, 1.0] + [0.0] * 382
        right = Chunk(document_id=doc.id, position=0, page_start=1, page_end=1,
                      text="Релейная защита предусмотрена проектом.",
                      embedding_model="dummy-384-v1")
        wrong = Chunk(document_id=doc.id, position=1, page_start=45, page_end=45,
                      text="Вспомогательное электрооборудование здания.",
                      embedding_model="dummy-384-v1")
        db.add_all([right, wrong])
        db.flush()
        for chunk, vector in ((right, v1), (wrong, v2)):
            db.execute(text("UPDATE chunks SET embedding=CAST(:v AS vector) WHERE id=:id"),
                       {"v": json.dumps(vector), "id": chunk.id})
        assert lexical_ids(db, doc.id, "релейная защита", 5)[0] == right.id
        assert semantic_ids(db, doc.id, "dummy-384-v1", v1, 5)[0] == right.id
        db.rollback()
    engine.dispose()


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="PostgreSQL job only")
def test_pg_requirement_validation_details_roundtrip():
    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    with Session(engine) as db:
        project = Project(name="validation details integration")
        db.add(project)
        db.flush()
        tu = Document(project_id=project.id, kind=DocumentKind.tu, filename="tu.docx", stored_path="none",
                      mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        znp = Document(project_id=project.id, kind=DocumentKind.znp, filename="znp.pdf", stored_path="none", mime_type="application/pdf")
        db.add_all([tu, znp])
        db.flush()
        requirement = TURequirement(project_id=project.id, document_id=tu.id, clause_number="1", category=RequirementCategory.other, text="Требование")
        db.add(requirement)
        db.flush()
        details = {"version": 1, "items": [{"item_id": 1, "evidence": [{"document_id": znp.id, "quote": "Цитата"}]}]}
        verification = RequirementVerification(project_id=project.id, requirement_id=requirement.id, znp_document_id=znp.id,
            status=RequirementStatus.needs_review, validation_details=details)
        db.add(verification)
        db.flush()
        db.expire(verification, ["validation_details"])
        assert verification.validation_details == details
        db.rollback()
    engine.dispose()
