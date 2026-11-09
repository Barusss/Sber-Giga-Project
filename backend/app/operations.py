"""Background operations whose progress survives browser refreshes.

Operations run in a server thread with their own database session. Each stage
is committed so the UI polls real state rather than a simulated timer. This
supports browser disconnects; process restarts require a separate task queue.
"""
import logging
import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from uuid import uuid4
from pathlib import Path
from threading import Lock

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import Headers

from app.core.config import settings
from app.core.db import SessionLocal, get_db
from app.core.models import (
    ComparisonPair, Document, DocumentKind, OperationJob, Project,
    ReviewState, now,
)
from app.documents.parser import extract_blocks
from app.documents.router import _read_upload, _save_document
from app.normatives.extraction import extract_normative_facts
from app.normatives.verification import DEFAULT_STANDARDS_BATCH, check_document_standards
from app.requirements.workspace import run_comparison as execute_comparison

router = APIRouter(tags=["operations"])
logger = logging.getLogger(__name__)
_enqueue_lock = Lock()
# Avoid exhausting SQLAlchemy's default 5+10 connection pool while long-running
# OCR / registry / LLM requests occupy database sessions. Pending jobs remain
# queued in PostgreSQL and do not open a database session until a worker starts.
_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="operation")
ACTIVE = ("queued", "running")
KINDS = ("normative_extract", "normative_check", "comparison", "comparison_batch")


class OperationRequest(BaseModel):
    kind: str
    document_id: str | None = None
    pair_ids: list[str] = Field(default_factory=list, max_length=100)
    refresh: bool = False


def _project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    if project.state == ReviewState.approved:
        raise HTTPException(409, "Approved project cannot be modified")
    return project


def _out(job: OperationJob) -> dict:
    return {
        "id": job.id, "project_id": job.project_id, "kind": job.kind,
        "status": job.status, "stage": job.stage, "message": job.message,
        "document_id": job.params.get("document_id"),
        "pair_ids": job.params.get("pair_ids", []),
        "filename": job.params.get("filename"),
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "error": job.error, "result": job.result,
        "created_at": job.created_at, "updated_at": job.updated_at,
        "completed_at": job.completed_at,
    }


def _update(db: Session, job_id: str, *, status: str | None = None,
            stage: str | None = None, message: str | None = None,
            current: int | None = None, total: int | None = None,
            result: dict | None = None, error: str | None = None):
    job = db.get(OperationJob, job_id)
    if job is None:
        return
    if status is not None:
        job.status = status
    if stage is not None:
        job.stage = stage
    if message is not None:
        job.message = message[:255]
    if current is not None:
        job.progress_current = current
    if total is not None:
        job.progress_total = total
    if result is not None:
        job.result = deepcopy(result)
    if error is not None:
        job.error = error[:2000]
    if status in ("completed", "failed", "cancelled"):
        job.completed_at = now()
    job.updated_at = now()
    db.commit()


def _cancelled(db: Session, job_id: str) -> bool:
    db.expire_all()
    return bool(db.get(OperationJob, job_id).cancel_requested)


def _run_upload(db: Session, job: OperationJob) -> dict:
    params = job.params
    path = Path(params["upload_path"])
    _project(db, job.project_id)
    _update(db, job.id, stage="parsing",
            message="Извлечение текста и распознавание сканированных страниц")
    data = path.read_bytes()
    blocks = extract_blocks(data, params["suffix"])
    if _cancelled(db, job.id):
        raise _JobCancelled()
    _update(db, job.id, stage="indexing",
            message="Сохранение документа и построение поискового индекса")
    upload = UploadFile(
        filename=params["filename"], file=BytesIO(data),
        headers=Headers({"content-type": params["mime_type"]}),
    )
    saved = _save_document(job.project_id, DocumentKind(params["kind"]), upload,
                           data, params["suffix"], blocks, db)
    return {"document_id": saved.id, "filename": saved.filename}


def _run_extract(db: Session, job: OperationJob) -> dict:
    document = db.get(Document, job.params["document_id"])
    _project(db, job.project_id)
    if document is None or document.project_id != job.project_id:
        raise HTTPException(404, "Document not found")
    _update(db, job.id, stage="extracting", message="Извлечение нормативных ссылок")
    result = extract_normative_facts(db, document)
    return result.model_dump(mode="json")


def _run_check(db: Session, job: OperationJob) -> dict:
    document = db.get(Document, job.params["document_id"])
    _project(db, job.project_id)
    if document is None or document.project_id != job.project_id:
        raise HTTPException(404, "Document not found")
    offset = 0
    totals = {
        "document_id": document.id, "total": 0, "active": 0,
        "obsolete": 0, "unknown": 0,
        "findings_created": 0, "findings_already_existing": 0, "checked": [],
    }
    while True:
        if _cancelled(db, job.id):
            raise _JobCancelled()
        _update(db, job.id, stage="verifying", message="Проверка нормативных ссылок")
        batch = check_document_standards(
            db, document, offset=offset, limit=DEFAULT_STANDARDS_BATCH,
            refresh=bool(job.params.get("refresh")),
        )
        if batch.offset != offset or batch.total != len(batch.checked):
            raise ValueError("Некорректный результат проверки нормативов")
        for key in ("total", "active", "obsolete", "unknown",
                    "findings_created", "findings_already_existing"):
            totals[key] += getattr(batch, key)
        totals["checked"].extend(item.model_dump(mode="json") for item in batch.checked)
        _update(
            db, job.id, stage="verifying",
            message=f"Проверено нормативов: {totals['total']} из {batch.document_total}",
            current=totals["total"], total=batch.document_total,
            result=totals,
        )
        if batch.next_offset is None:
            if totals["total"] != batch.document_total:
                raise ValueError("Не совпало количество проверенных нормативов")
            return totals
        if batch.next_offset <= offset:
            raise ValueError("Проверка нормативов не продвигается")
        offset = batch.next_offset


COMPARISON_STAGES = {
    "reading": "Чтение и разбор пунктов ТУ и ЗнП",
    "preparing": "Формирование перечня требований ТУ",
    "embedding": "Подготовка поиска соответствий в ЗнП",
    "verifying": "Сопоставление требований ТУ с ЗнП",
    "persisting": "Формирование и сохранение замечаний",
    "snapshot": "Сохранение результата в историю",
}


def _run_comparisons(db: Session, job: OperationJob) -> dict:
    pair_ids = job.params["pair_ids"]
    results = []
    last_progress = None
    for index, pair_id in enumerate(pair_ids):
        if _cancelled(db, job.id):
            raise _JobCancelled()
        _update(db, job.id, stage="reading",
                message=f"Подготовка пары {index + 1} из {len(pair_ids)}",
                current=index, total=len(pair_ids))
        started_verifying = None
        last_processed = 0

        def report(stage: str, current: int, total: int, clause_number: str | None):
            nonlocal started_verifying, last_processed, last_progress
            if stage == "verifying" and started_verifying is None:
                started_verifying = time.monotonic()
            if stage == "verifying" and current > last_processed:
                last_processed = current

            elapsed = (
                time.monotonic() - started_verifying
                if started_verifying is not None else 0.0
            )
            estimate = (
                int(elapsed / current * (total - current))
                if stage == "verifying" and current >= 3 and elapsed >= 5
                and 0 < current < total else None
            )
            last_progress = {
                "pair_id": pair_id,
                "pair_index": index + 1,
                "pair_total": len(pair_ids),
                "stage": stage,
                "requirements_current": current if total > 0 else None,
                "requirements_total": total if total > 0 else None,
                "current_clause": clause_number,
                "remaining_seconds": estimate,
            }
            message = COMPARISON_STAGES[stage]
            if stage == "verifying" and clause_number:
                message = f"Анализ пункта ТУ {clause_number}"
            # Independent short transaction: the analysis session may hold a
            # project lock and uncommitted findings. Never commit that session
            # just to publish progress or silently persist partial analysis.
            with SessionLocal() as progress_db:
                _update(progress_db, job.id, stage=stage, message=message,
                        result={"runs": list(results), "comparison_progress": last_progress})

        result = execute_comparison(
            job.project_id, pair_id, db, progress_callback=report,
        )
        results.append({"pair_id": pair_id, "run_id": result["run"]["id"]})
        _update(db, job.id, stage="comparing",
                message=f"Выполнено сверок: {index + 1} из {len(pair_ids)}",
                current=index + 1, total=len(pair_ids),
                result={"runs": results, "comparison_progress": last_progress})
    return {"runs": results, "comparison_progress": last_progress}


class _JobCancelled(Exception):
    pass


def _worker(job_id: str):
    with SessionLocal() as db:
        job = db.get(OperationJob, job_id)
        if job is None:
            return
        try:
            _update(db, job_id, status="running", stage="starting",
                    message="Подготовка операции")
            if job.kind == "upload":
                result = _run_upload(db, job)
            elif job.kind == "normative_extract":
                result = _run_extract(db, job)
            elif job.kind == "normative_check":
                result = _run_check(db, job)
            else:
                result = _run_comparisons(db, job)
            _update(db, job_id, status="completed", stage="complete",
                    message="Операция завершена", result=result)
        except _JobCancelled:
            db.rollback()
            _update(db, job_id, status="cancelled", stage="cancelled",
                    message="Операция остановлена")
        except Exception as exc:
            logger.exception("Background operation failed: %s", job_id)
            db.rollback()
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            _update(db, job_id, status="failed", stage="error",
                    message="Операция завершилась с ошибкой", error=str(message))
        finally:
            if job.kind == "upload":
                Path(job.params["upload_path"]).unlink(missing_ok=True)


def _enqueue(db: Session, project_id: str, kind: str, params: dict) -> OperationJob:
    with _enqueue_lock:
        active = db.scalars(
            select(OperationJob).where(
                OperationJob.project_id == project_id,
                OperationJob.kind == kind,
                OperationJob.status.in_(ACTIVE),
            )
        ).all()
        for job in active:
            if kind in ("upload", "normative_extract", "normative_check"):
                if kind == "upload" or job.params.get("document_id") == params.get("document_id"):
                    raise HTTPException(409, "Операция уже выполняется")
            elif set(job.params.get("pair_ids", [])) & set(params.get("pair_ids", [])):
                raise HTTPException(409, "Эта сверка уже выполняется")
        if kind in ("comparison", "comparison_batch"):
            other_kind = "comparison" if kind == "comparison_batch" else "comparison_batch"
            for job in db.scalars(select(OperationJob).where(
                OperationJob.project_id == project_id, OperationJob.kind == other_kind,
                OperationJob.status.in_(ACTIVE),
            )).all():
                if set(job.params.get("pair_ids", [])) & set(params.get("pair_ids", [])):
                    raise HTTPException(409, "Эта сверка уже выполняется")
        job = OperationJob(
            project_id=project_id, kind=kind, params=params, stage="queued",
            status="queued", message="Ожидает выполнения",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        _workers.submit(_worker, job.id)
        return job


@router.post("/projects/{project_id}/operations/upload", status_code=202)
async def enqueue_upload(project_id: str, kind: DocumentKind = Form(...),
                         file: UploadFile = File(...), db: Session = Depends(get_db)):
    data, suffix = await _read_upload(project_id, file, db)
    temp_id = uuid4().hex
    path = settings.upload_dir / "pending" / f"{temp_id}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    try:
        job = _enqueue(db, project_id, "upload", {
            "kind": kind.value, "filename": Path(file.filename or "document").name[:255],
            "mime_type": file.content_type or "application/octet-stream",
            "suffix": suffix, "upload_path": str(path),
        })
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return _out(job)


@router.post("/projects/{project_id}/operations", status_code=202)
def enqueue_operation(project_id: str, payload: OperationRequest, db: Session = Depends(get_db)):
    _project(db, project_id)
    if payload.kind not in KINDS:
        raise HTTPException(422, "Unsupported operation")
    if payload.kind in ("normative_extract", "normative_check"):
        document = db.get(Document, payload.document_id) if payload.document_id else None
        if document is None or document.project_id != project_id:
            raise HTTPException(422, "Document must belong to this project")
        params = {"document_id": document.id, "refresh": payload.refresh}
    else:
        if not payload.pair_ids or (payload.kind == "comparison" and len(payload.pair_ids) != 1):
            raise HTTPException(422, "Select the comparison pair or pairs")
        pairs = db.scalars(select(ComparisonPair).where(
            ComparisonPair.project_id == project_id,
            ComparisonPair.id.in_(payload.pair_ids),
        )).all()
        if len(pairs) != len(set(payload.pair_ids)):
            raise HTTPException(422, "All pairs must belong to this project")
        params = {"pair_ids": list(dict.fromkeys(payload.pair_ids))}
    return _out(_enqueue(db, project_id, payload.kind, params))


@router.get("/projects/{project_id}/operations")
def list_operations(project_id: str, db: Session = Depends(get_db)):
    if db.get(Project, project_id) is None:
        raise HTTPException(404, "Project not found")
    jobs = db.scalars(
        select(OperationJob).where(OperationJob.project_id == project_id)
        .order_by(OperationJob.created_at.desc()).limit(40)
    ).all()
    return [_out(job) for job in jobs]


@router.get("/projects/{project_id}/operations/{job_id}")
def get_operation(project_id: str, job_id: str, db: Session = Depends(get_db)):
    job = db.get(OperationJob, job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(404, "Operation not found")
    return _out(job)


@router.post("/projects/{project_id}/operations/{job_id}/cancel")
def cancel_operation(project_id: str, job_id: str, db: Session = Depends(get_db)):
    job = db.get(OperationJob, job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(404, "Operation not found")
    if job.status in ACTIVE and not job.cancel_requested:
        job.cancel_requested = True
        job.message = "Остановка после текущего этапа"
        db.commit()
    return _out(job)
