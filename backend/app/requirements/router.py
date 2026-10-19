"""Storage and human review API for the participant #3 TU/ZnP module.

Manual input and expert confirmations are usable now. Automated document
comparison remains intentionally unimplemented until LLM evidence checking is
integrated, so no made-up analysis results can enter the project report.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.models import (
    Document, DocumentKind, Project, RequirementStatus, RequirementVerification,
    ReviewState, TURequirement,
)
from app.requirements.schemas import (
    ConfirmRequirementRequest, RequirementVerificationOut,
    TURequirementCreate, TURequirementOut,
)

router = APIRouter(prefix="/requirements", tags=["requirements"])


def get_project(db: Session, project_id: str, edit: bool = False) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    if edit and project.state == ReviewState.approved:
        raise HTTPException(409, "Approved project cannot be modified")
    return project


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
    item = TURequirement(
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
    stmt = select(TURequirement).where(TURequirement.project_id == project_id)
    if document_id is not None:
        document = db.get(Document, document_id)
        if document is None or document.project_id != project_id:
            raise HTTPException(404, "Document not found in project")
        stmt = stmt.where(TURequirement.document_id == document_id)
    return db.scalars(stmt.order_by(TURequirement.created_at, TURequirement.id)).all()


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
    get_project(db, project_id, edit=True)
    requirement = db.get(TURequirement, payload.requirement_id)
    if requirement is None or requirement.project_id != project_id:
        raise HTTPException(404, "TU requirement not found in project")
    verification = db.scalar(
        select(RequirementVerification)
        .where(RequirementVerification.project_id == project_id,
               RequirementVerification.requirement_id == requirement.id)
        .order_by(RequirementVerification.created_at.desc(),
                  RequirementVerification.id.desc())
    )
    if verification is None:
        verification = RequirementVerification(
            project_id=project_id, requirement_id=requirement.id,
            status=RequirementStatus.needs_review,
            confidence_score=None,
        )
        db.add(verification)
    verification.is_confirmed_by_human = True
    verification.human_override_status = RequirementStatus(payload.status.value)
    verification.human_comment = payload.comment
    db.commit()
    db.refresh(verification)
    return verification


@router.post("/analyze", status_code=501)
def analyze_requirements():
    raise HTTPException(
        501,
        "Semantic matching of TU and ZnP is not implemented yet; manual storage and review are available",
    )
