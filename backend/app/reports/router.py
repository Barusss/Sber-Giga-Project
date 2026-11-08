from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.models import Finding, Project, ReviewState, ReviewStatus, Document
from app.core.models import Chunk, DocumentKind, Fact, NormativeVerification, RequirementVerification, TURequirement, RequirementAnalysis
from app.normatives.extraction import normalize_code
from app.core.schemas import FindingCreate, FindingOut, FindingReview
from app.reports.normative_report import NormativeReport, build_normative_report
from app.reports.requirements_report import build_requirements_report

router = APIRouter(tags=["reports"])

def project_or_404(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    return project

@router.get("/projects/{project_id}/findings", response_model=list[FindingOut])
def list_findings(project_id: str, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    return db.scalars(select(Finding).where(Finding.project_id == project_id).order_by(Finding.created_at)).all()

@router.post("/projects/{project_id}/findings", response_model=FindingOut, status_code=201)
def create_finding(project_id: str, data: FindingCreate, db: Session = Depends(get_db)):
    project = project_or_404(db, project_id)
    if project.state != ReviewState.draft:
        raise HTTPException(409, "Project approved")
    ids = {d.id for d in db.scalars(select(Document).where(Document.project_id == project_id)).all()}
    if any(item.document_id not in ids for item in data.evidence):
        raise HTTPException(422, "Evidence document must belong to project")
    finding = Finding(project_id=project_id, **data.model_dump(mode="json"))
    db.add(finding)
    db.commit()
    db.refresh(finding)
    return finding

@router.patch("/findings/{finding_id}/review", response_model=FindingOut)
def review_finding(finding_id: str, data: FindingReview, db: Session = Depends(get_db)):
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(404, "Finding not found")
    if project_or_404(db, finding.project_id).state != ReviewState.draft:
        raise HTTPException(409, "Project approved")
    finding.review_status = ReviewStatus(data.review_status)
    db.commit()
    db.refresh(finding)
    return finding

@router.post("/projects/{project_id}/approve")
def approve_project(project_id: str, db: Session = Depends(get_db)):
    project = project_or_404(db, project_id)
    if project.state == ReviewState.approved:
        raise HTTPException(409, "Already approved")
    documents = db.scalars(select(Document).where(Document.project_id == project_id)).all()
    if not documents or any(not db.scalar(select(Chunk.id).where(Chunk.document_id == d.id).limit(1)) for d in documents):
        raise HTTPException(409, "Every document must have useful indexed text before approval")
    tu_ids = {d.id for d in documents if d.kind == DocumentKind.tu}
    znp_ids = {d.id for d in documents if d.kind == DocumentKind.znp}
    analyses = db.scalars(select(RequirementAnalysis).where(
        RequirementAnalysis.tu_document_id.in_(tu_ids),
        RequirementAnalysis.znp_document_id.in_(znp_ids),
    )).all() if tu_ids and znp_ids else []
    if tu_ids or znp_ids:
        if (not tu_ids or not znp_ids
                or not tu_ids <= {run.tu_document_id for run in analyses}
                or not znp_ids <= {run.znp_document_id for run in analyses}):
            raise HTTPException(409, "Complete TU/ZNP analysis for every document before approval")
        for run in analyses:
            ids = set(run.requirement_ids)
            verified = db.scalars(select(RequirementVerification).where(
                RequirementVerification.requirement_id.in_(ids),
                RequirementVerification.znp_document_id == run.znp_document_id,
            )).all()
            if not ids or ids != {v.requirement_id for v in verified}:
                raise HTTPException(409, "Comparison results are incomplete; re-run analysis")
            if any(not v.is_confirmed_by_human and (v.status.value == "needs_review" or v.method != "llm") for v in verified):
                raise HTTPException(409, "Uncertain requirements need an engineer decision")
    for document in documents:
        # A comparison covers TU/ZNP documents. Standalone normative documents
        # need a completed extraction even if no references were found.
        facts = db.scalars(select(Fact).where(Fact.document_id == document.id,
            Fact.fact_type == "normative_reference", Fact.review_status != ReviewStatus.rejected)).all()
        if document.kind == DocumentKind.other and not document.normative_extraction_completed:
            raise HTTPException(409, "Complete normative extraction before approval")
        checked = set(db.scalars(select(NormativeVerification.code).where(
            NormativeVerification.document_id == document.id)))
        if any(normalize_code(f.value.get("code", "")) not in checked for f in facts):
            raise HTTPException(409, "Complete normative verification before approval")
    pending = db.scalar(select(func.count()).select_from(Finding).where(Finding.project_id == project_id, Finding.review_status == ReviewStatus.pending))
    if pending:
        raise HTTPException(409, "All findings must be reviewed before approval")
    project.state = ReviewState.approved
    db.commit()
    return {"state": "approved"}

@router.get("/projects/{project_id}/normative-report", response_model=NormativeReport)
def normative_report(project_id: str, document_id: str | None = None,
                     db: Session = Depends(get_db)):
    project = project_or_404(db, project_id)
    if document_id is not None:
        document = db.get(Document, document_id)
        if document is None or document.project_id != project_id:
            raise HTTPException(404, "Document not found in this project")
    return build_normative_report(db, project, document_id=document_id)


@router.get("/projects/{project_id}/expert-report")
def expert_report(project_id: str, document_id: str | None = None, tu_document_id: str | None = None,
                  znp_document_id: str | None = None, db: Session = Depends(get_db)):
    project = project_or_404(db, project_id)
    if document_id is not None:
        document = db.get(Document, document_id)
        if document is None or document.project_id != project_id:
            raise HTTPException(404, "Document not found in this project")
    if (tu_document_id is None) != (znp_document_id is None):
        raise HTTPException(422, "Select both TU and ZNP documents")
    try:
        requirements = build_requirements_report(db, project_id, tu_document_id, znp_document_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    normatives = build_normative_report(db, project, document_id=document_id)
    return {"requirements": requirements, "normatives": normatives}


@router.get("/projects/{project_id}/report")
def project_report(project_id: str, db: Session = Depends(get_db)):
    project = project_or_404(db, project_id)
    findings = db.scalars(select(Finding).where(Finding.project_id == project_id)).all()
    return {"project_id": project.id, "project_name": project.name, "state": project.state, "findings": [FindingOut.model_validate(item).model_dump(mode="json") for item in findings]}
