"""Durable project-level normative expert report.

Reads only document facts, verified per-document snapshots and existing findings.
Never extrapolates a registry status from another document's cached record.
"""
from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.models import (
    Document, Fact, FactEvidence, Finding, NormativeVerification, Project, ReviewStatus,
)
from app.normatives.extraction import normalize_code
from app.normatives.verification import FINDING_TYPE, finding_key


class QuoteOut(BaseModel):
    fragment_id: str
    page: int
    quote: str


class FindingReviewOut(BaseModel):
    id: str
    review_status: str
    severity: str
    explanation: str
    suggested_action: str | None


class NormativeReportItem(BaseModel):
    document_id: str
    document_name: str
    code: str
    reference_kind: str
    status: str
    registry_status: str | None
    title: str | None
    replacement: str | None
    source_url: str | None
    checked_at: datetime | None
    attempted_at: datetime | None
    reason: str | None
    section: str
    evidence: list[QuoteOut]
    finding: FindingReviewOut | None


class NormativeReport(BaseModel):
    project_id: str
    project_name: str
    project_state: str
    selected_document_id: str | None = None
    generated_at: datetime
    total: int
    requires_attention: int
    informative: int
    unverified: int
    active: int
    items: list[NormativeReportItem]


def build_normative_report(db: Session, project: Project, document_id: str | None = None) -> NormativeReport:
    query = select(Document).where(Document.project_id == project.id)
    if document_id is not None:
        query = query.where(Document.id == document_id)
    documents = db.scalars(query.order_by(Document.created_at, Document.id)).all()
    doc_by_id = {doc.id: doc for doc in documents}
    doc_ids = list(doc_by_id)
    if not doc_ids:
        return NormativeReport(
            project_id=project.id, project_name=project.name, project_state=project.state.value, selected_document_id=document_id,
            generated_at=datetime.now(timezone.utc), total=0,
            requires_attention=0, informative=0, unverified=0, active=0, items=[],
        )

    facts = db.scalars(
        select(Fact)
        .where(Fact.document_id.in_(doc_ids), Fact.fact_type == "normative_reference",
               Fact.review_status != ReviewStatus.rejected)
        .options(selectinload(Fact.evidence).selectinload(FactEvidence.fragment))
    ).all()
    snapshots = {
        (item.document_id, item.code): item for item in db.scalars(
            select(NormativeVerification)
            .where(NormativeVerification.document_id.in_(doc_ids))
        ).all()
    }
    findings = db.scalars(
        select(Finding).where(
            Finding.project_id == project.id,
            Finding.module == "normatives",
            Finding.type == FINDING_TYPE,
        )
    ).all()

    grouped: dict[tuple[str, str], list[Fact]] = {}
    for fact in facts:
        if not isinstance(fact.value, dict):
            continue
        raw_code = fact.value.get("code")
        if isinstance(raw_code, str) and raw_code.strip():
            key = (fact.document_id, normalize_code(raw_code))
            grouped.setdefault(key, []).append(fact)

    items = []
    for (doc_id, code), related in sorted(grouped.items()):
        snapshot = snapshots.get((doc_id, code))
        role = "informative"
        if any(fact.value.get("reference_kind") != "informative" for fact in related):
            role = "normative"
        status = snapshot.status if snapshot else "unknown"
        if status not in {"active", "obsolete"}:
            status = "unknown"

        if status == "unknown":
            section = "unverified"
        elif status == "active":
            section = "active"
        elif role == "informative":
            section = "informative"
        else:
            section = "requires_attention"

        quoted = {}
        for fact in related:
            for evidence in fact.evidence:
                quoted[(evidence.fragment_id, evidence.quote)] = QuoteOut(
                    fragment_id=evidence.fragment_id,
                    page=evidence.fragment.page,
                    quote=evidence.quote,
                )
        citations = sorted(quoted.values(), key=lambda item: (item.page, item.fragment_id))

        finding = next((f for f in findings if finding_key(f, doc_id, code)), None)
        finding_out = FindingReviewOut(
            id=finding.id, review_status=finding.review_status.value,
            severity=finding.severity, explanation=finding.explanation,
            suggested_action=finding.suggested_action,
        ) if finding is not None else None
        items.append(NormativeReportItem(
            document_id=doc_id, document_name=doc_by_id[doc_id].filename,
            code=code, reference_kind=role, status=status,
            registry_status=snapshot.registry_status if snapshot else None,
            title=snapshot.title if snapshot else None,
            replacement=snapshot.replacement if snapshot else None,
            source_url=snapshot.source_url if snapshot else None,
            checked_at=snapshot.checked_at if snapshot else None,
            attempted_at=snapshot.attempted_at if snapshot else None,
            reason=(snapshot.reason if snapshot else "not_yet_verified"),
            section=section, evidence=citations, finding=finding_out,
        ))

    return NormativeReport(
        project_id=project.id, project_name=project.name, project_state=project.state.value, selected_document_id=document_id,
        generated_at=datetime.now(timezone.utc), total=len(items),
        requires_attention=sum(item.section == "requires_attention" for item in items),
        informative=sum(item.section == "informative" for item in items),
        unverified=sum(item.section == "unverified" for item in items),
        active=sum(item.section == "active" for item in items),
        items=items,
    )
