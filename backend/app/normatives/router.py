from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.models import Normative
from app.core.schemas import NormativeCreate, NormativeOut

router = APIRouter(prefix="/normatives", tags=["normatives"])

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
