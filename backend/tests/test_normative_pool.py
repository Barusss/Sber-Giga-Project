"""External registry lookups must not occupy the whole SQLAlchemy connection pool."""
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.db import Base
from app.core.models import (
    Document, DocumentKind, Fact, FactEvidence, Fragment, Project,
)
from app.normatives.registry import RegistryResult
from app.normatives.verification import check_document_standards


def test_official_lookup_returns_connection_to_pool(tmp_path):
    # With a one-connection pool, any database access while the registry
    # lookup is waiting fails unless the verification releases its transaction.
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'registry-pool.sqlite'}",
        pool_size=1, max_overflow=0, pool_timeout=0.2,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as db:
            project = Project(name="Проверка подключения")
            document = Document(
                project=project, kind=DocumentKind.tu, filename="tu.pdf",
                stored_path="/tmp/unused.pdf", mime_type="application/pdf",
            )
            fragment = Fragment(
                document=document, page=1, position=0,
                text="См. ГОСТ 29322-92",
            )
            fact = Fact(
                document=document, fact_type="normative_reference",
                value={"code": "ГОСТ 29322-92", "reference_kind": "normative"},
            )
            fact.evidence.append(FactEvidence(fragment=fragment, quote="ГОСТ 29322-92"))
            db.add_all([project, document, fragment, fact])
            db.commit()
            document_id = document.id
            project_id = project.id

        class Registry:
            def lookup(self, code, source_url=None):
                with Session(engine) as another_request:
                    assert another_request.scalar(
                        select(Project.id).where(Project.id == project_id)
                    ) == project_id
                return RegistryResult(code=code, reason="official_verification_unavailable")

        with Session(engine, expire_on_commit=False) as checking:
            document = checking.get(Document, document_id)
            report = check_document_standards(checking, document, registry=Registry())
            assert report.total == report.document_total == 1
            assert report.checked[0].status == "unknown"
    finally:
        engine.dispose()
