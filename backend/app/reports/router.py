from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.models import Finding, Project, ReviewState, ReviewStatus, Document
from app.core.schemas import FindingCreate, FindingOut, FindingReview

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
    pending = db.scalar(select(func.count()).select_from(Finding).where(Finding.project_id == project_id, Finding.review_status == ReviewStatus.pending))
    if pending:
        raise HTTPException(409, "All findings must be reviewed before approval")
    project.state = ReviewState.approved
    db.commit()
    return {"state": "approved"}

@router.get("/projects/{project_id}/report")
def project_report(project_id: str, db: Session = Depends(get_db)):
    project = project_or_404(db, project_id)
    findings = db.scalars(select(Finding).where(Finding.project_id == project_id)).all()
    return {"project_id": project.id, "project_name": project.name, "state": project.state, "findings": [FindingOut.model_validate(item).model_dump(mode="json") for item in findings]}
