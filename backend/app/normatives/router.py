from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.models import Normative, Document, Project, ReviewState
from app.normatives.extraction import ExtractionResult, extract_normative_facts
from app.core.schemas import NormativeCreate, NormativeOut

router = APIRouter(prefix="/normatives", tags=["normatives"])
extraction_router = APIRouter(tags=["normatives"])

@router.get("", response_model=list[NormativeOut])
def list_normatives(db: Session = Depends(get_db)):
    return db.scalars(select(Normative).order_by(Normative.code)).all()

@router.post("", response_model=NormativeOut, status_code=201)
def add_normative(payload: NormativeCreate, db: Session = Depends(get_db)):
    normative = Normative(**payload.model_dump())
    db.add(normative)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Normative code already exists") from exc
    db.refresh(normative)
    return normative

@router.get("/{code}", response_model=NormativeOut)
def get_normative(code: str, db: Session = Depends(get_db)):
    normative = db.scalar(select(Normative).where(Normative.code == code))
    if normative is None:
        raise HTTPException(404, "Normative not found")
    return normative


@extraction_router.post("/documents/{document_id}/extract-facts", response_model=ExtractionResult)
def extract_facts(document_id: str, db: Session = Depends(get_db)):
    """Extract cited norms using DeepSeek, with verified fragment-level evidence."""
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    if db.get(Project, document.project_id).state == ReviewState.approved:
        raise HTTPException(409, "Approved project cannot be modified")
    return extract_normative_facts(db, document)
