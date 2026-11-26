"""HTTP endpoints for TU/ZnP requirements, verification and human review."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.models import (
    Document, DocumentKind, RequirementVerification, TURequirement as DBTURequirement,
)
from app.documents.parser import TABLE_REVIEW
from app.requirements.agent import RequirementsAgent
from app.requirements.analysis import (
    analyze_project_requirements as _analyze_project_requirements,
    _attach_provenance, _fragment_for_quote, _apply_quote_provenance,
)
from app.requirements.schemas import (
    AnalyzeProjectRequirementsRequest, CheckRequirementsRequest, CheckRequirementsResponse,
    ConfirmRequirementRequest, RequirementVerificationOut, TURequirementCreate, TURequirementOut,
)
from app.requirements.storage import (
    get_project, load_project_requirements, analysis_lock as _analysis_lock,
    lock_project_for_analysis as _lock_project_for_analysis,
    confirm_requirement as _confirm_requirement,
)
from app.requirements.znp_first import ZnPFirstAgent

router = APIRouter(prefix="/requirements", tags=["requirements"])


@router.post("/projects/{project_id}/tu-requirements",
             response_model=TURequirementOut, status_code=201)
def create_tu_requirement(
    project_id: str, payload: TURequirementCreate, db: Session = Depends(get_db),
):
    """Register a manually prepared TU requirement; NOT AI extraction."""
    get_project(db, project_id, edit=True)
    document = db.get(Document, payload.document_id)
    if document is None or document.project_id != project_id:
        raise HTTPException(404, "TU document not found in project")
    if document.kind != DocumentKind.tu:
        raise HTTPException(422, "Source document must have kind=tu")
    item = DBTURequirement(
        project_id=project_id,
        document_id=document.id,
        clause_number=payload.clause_number.strip(),
        category=payload.category,
        text=payload.text.strip(),
    )
    if not item.clause_number or not item.text:
        raise HTTPException(422, "Requirement text and clause number cannot be blank")
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.get("/projects/{project_id}/tu-requirements", response_model=list[TURequirementOut])
def list_tu_requirements(
    project_id: str, document_id: str | None = None, db: Session = Depends(get_db),
):
    get_project(db, project_id)
    stmt = select(DBTURequirement).where(DBTURequirement.project_id == project_id)
    if document_id is not None:
        document = db.get(Document, document_id)
        if document is None or document.project_id != project_id:
            raise HTTPException(404, "Document not found in project")
        stmt = stmt.where(DBTURequirement.document_id == document_id)
    return db.scalars(
        stmt.order_by(DBTURequirement.created_at, DBTURequirement.id)
    ).all()


@router.get("/projects/{project_id}/verifications",
            response_model=list[RequirementVerificationOut])
def list_verifications(project_id: str, db: Session = Depends(get_db)):
    get_project(db, project_id)
    return db.scalars(
        select(RequirementVerification)
        .where(RequirementVerification.project_id == project_id)
        .order_by(RequirementVerification.created_at, RequirementVerification.id)
    ).all()


@router.post("/projects/{project_id}/confirm",
             response_model=RequirementVerificationOut)
def confirm_requirement(
    project_id: str, payload: ConfirmRequirementRequest, db: Session = Depends(get_db),
):
    """Record an engineer's manual verdict, without pretending it came from AI.

    Reuses a stored verification (when any) and preserves its original AI
    status/explanation while setting the human override. If no verification
    exists, creates a needs_review record with a human override.
    """
    with _analysis_lock(project_id):
        _lock_project_for_analysis(db, project_id)
        return _confirm_requirement(project_id, payload, db)


@router.post("/analyze", response_model=CheckRequirementsResponse)
def analyze_requirements(
    payload: AnalyzeProjectRequirementsRequest | CheckRequirementsRequest,
    db: Session = Depends(get_db),
):
    """Run the agent-based TU/ZnP comparison.

    Two scenarios are supported:
    - explicit lists of TU requirements and ZnP clauses (tests, benchmarks);
    - a stored project with TU and ZnP documents (atomic clauses from fragments).
    """
    if isinstance(payload, CheckRequirementsRequest):
        if any(TABLE_REVIEW in item.warnings for item in (*payload.tu_requirements, *payload.znp_clauses)):
            agent = ZnPFirstAgent()
        else:
            agent = RequirementsAgent()
        response = agent.verify_all(payload.tu_requirements, payload.znp_clauses)
        _attach_provenance(
            response.checked_requirements,
            payload.tu_requirements,
            payload.znp_clauses,
        )
        return response

    project_id = payload.project_id
    agent = ZnPFirstAgent()

    with _analysis_lock(project_id):
        _lock_project_for_analysis(db, project_id)
        return _analyze_project_requirements(payload, db, agent)


@router.get("/projects/{project_id}", response_model=CheckRequirementsResponse)
def get_project_requirements(project_id: str, tu_document_id: str | None = None,
                             znp_document_id: str | None = None, db: Session = Depends(get_db)):
    """Return persisted TU/ZnP comparison results for a project."""
    return load_project_requirements(db, project_id, tu_document_id, znp_document_id)
