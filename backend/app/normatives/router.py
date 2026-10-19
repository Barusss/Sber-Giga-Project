from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, or_, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.models import Normative, Document, Project, ReviewState, NormativeVerification, Finding
from app.normatives.extraction import ExtractionResult, extract_normative_facts
from app.normatives.verification import VerificationReport, check_document_standards
from app.core.schemas import (
    NormativeCreate, 
    NormativeUpdate, 
    NormativeOut
)

# Основной роутер для управления реестром
router = APIRouter(prefix="/normatives", tags=["normatives"])

# Роутер для извлечения и проверки (существующая логика)
extraction_router = APIRouter(tags=["normatives"])


# ==============================================================================
# 1. УЛУЧШЕННОЕ УПРАВЛЕНИЕ РЕЕСТРОМ (PR 1)
# ==============================================================================

@router.get("", response_model=list[NormativeOut])
def list_normatives(
    search: str | None = None, 
    status: str | None = None, 
    db: Session = Depends(get_db)
):
    """Получить список нормативов с возможностью поиска и фильтрации по статусу."""
    query = select(Normative)
    if search:
        pattern = f"%{search}%"
        query = query.where(or_(Normative.code.ilike(pattern), Normative.title.ilike(pattern)))
    if status:
        query = query.where(Normative.status == status)
    
    return db.scalars(query.order_by(Normative.code)).all()


@router.post("", response_model=NormativeOut, status_code=201)
def add_normative(payload: NormativeCreate, db: Session = Depends(get_db)):
    """Добавить новый норматив в реестр."""
    normative = Normative(**payload.model_dump())
    db.add(normative)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Норматив с таким обозначением уже существует")
    db.refresh(normative)
    return normative


@router.patch("/{normative_id}", response_model=NormativeOut)
def update_normative(normative_id: str, payload: NormativeUpdate, db: Session = Depends(get_db)):
    """Частичное обновление данных норматива (редактирование)."""
    normative = db.scalar(select(Normative).where(Normative.id == normative_id))
    if not normative:
        raise HTTPException(404, "Норматив не найден")
    
    # Обновляем только те поля, которые были переданы в запросе
    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(normative, field, value)
        
    db.commit()
    db.refresh(normative)
    return normative


@router.delete("/{normative_id}", status_code=204)
def delete_normative(normative_id: str, db: Session = Depends(get_db)):
    """Безопасное удаление норматива. Запрещено, если он участвовал в проверках или замечаниях."""
    normative = db.scalar(select(Normative).where(Normative.id == normative_id))
    if not normative:
        raise HTTPException(404, "Норматив не найден")
    
    # Проверяем историю: есть ли записи верификации или замечания с этим кодом
    has_verification = db.scalar(
        select(NormativeVerification).where(NormativeVerification.code == normative.code).limit(1)
    )
    has_finding = db.scalar(
        select(Finding).where(
            Finding.module == "normatives", 
            Finding.explanation.ilike(f"%{normative.code}%")
        ).limit(1)
    )
    
    if has_verification or has_finding:
        raise HTTPException(
            409, 
            f"Невозможно удалить: норматив {normative.code} уже участвовал в проверках или замечаниях. История должна быть сохранена."
        )
    
    db.execute(delete(Normative).where(Normative.id == normative_id))
    db.commit()
    return None


@router.get("/{code}", response_model=NormativeOut)
def get_normative(code: str, db: Session = Depends(get_db)):
    """Получить норматив по его обозначению."""
    normative = db.scalar(select(Normative).where(Normative.code == code))
    if normative is None:
        raise HTTPException(404, "Normative not found")
    return normative


# ==============================================================================
# 2. СУЩЕСТВУЮЩАЯ ЛОГИКА ИЗВЛЕЧЕНИЯ И ПРОВЕРКИ (НЕ ТРОГАЕМ)
# ==============================================================================

@extraction_router.post("/documents/{document_id}/extract-facts", response_model=ExtractionResult)
def extract_facts(document_id: str, db: Session = Depends(get_db)):
    """Извлечь нормативные ссылки из документа с помощью DeepSeek."""
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    if db.get(Project, document.project_id).state == ReviewState.approved:
        raise HTTPException(409, "Approved project cannot be modified")
    return extract_normative_facts(db, document)


@extraction_router.post("/documents/{document_id}/check-normatives", response_model=VerificationReport)
def check_normatives(document_id: str, db: Session = Depends(get_db)):
    """Проверить извлеченные стандарты по официальному реестру Росстандарта."""
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    if db.get(Project, document.project_id).state == ReviewState.approved:
        raise HTTPException(409, "Approved project cannot be modified")
    return check_document_standards(db, document)