"""Background operations whose progress survives browser refreshes.

Operations run in a server thread with their own database session. Progress is
committed for browser polling. Comparisons additionally checkpoint each LLM
response and can be paused/resumed across restarts in the single-API deployment;
other operation kinds still use the existing non-resumable worker.
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
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from starlette.datastructures import Headers

from app.core.config import settings
from app.core.db import SessionLocal, get_db
from app.core.models import (
    ComparisonPair, ComparisonRun, ComparisonAtomReview, ComparisonLLMCheckpoint,
    Document, DocumentKind, OperationJob, Project, ReviewState, now,
)
from app.documents.parser import extract_blocks
from app.documents.router import _read_upload, _save_document
from app.normatives.extraction import extract_normative_facts
from app.normatives.verification import DEFAULT_STANDARDS_BATCH, check_document_standards
from app.requirements.workspace import run_comparison as execute_comparison
from app.requirements.atom_review import run_atom_recheck
from app.requirements.checkpoints import (
    ComparisonPause, ComparisonCancelled, checkpoint_counts,
)

router = APIRouter(tags=["operations"])
logger = logging.getLogger(__name__)
_enqueue_lock = Lock()
# Avoid exhausting SQLAlchemy's default 5+10 connection pool while long-running
# OCR / registry / LLM requests occupy database sessions. Pending jobs remain
# queued in PostgreSQL and do not open a database session until a worker starts.
_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="operation")
ACTIVE = ("queued", "running", "paused")
COMPARISON_KINDS = ("comparison", "comparison_batch", "comparison_atom_recheck")
KINDS = ("normative_extract", "normative_check", *COMPARISON_KINDS)


class ResumeRequest(BaseModel):
    accept_replay_risk: bool = False


class OperationRequest(BaseModel):
    kind: str
    document_id: str | None = None
    pair_ids: list[str] = Field(default_factory=list, max_length=100)
    refresh: bool = False
    test_mode: bool = False
    run_id: str | None = None
    atom_id: str | None = None


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
        "test_mode": bool(job.params.get("test_mode", False)),
        "run_id": job.params.get("run_id"),
        "atom_id": job.params.get("atom_id"),
        "pause_requested": bool(job.pause_requested),
        "checkpoint": (
            checkpoint_counts(job.id, SessionLocal)
            if job.kind in COMPARISON_KINDS else None
        ),
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
    "embedding": "Подготовка поиска соответствий ЗнП → ТУ",
    "verifying": "Сопоставление положений ЗнП с требованиями ТУ",
    "coverage": "Проверка покрытия требований ТУ и дополнительный поиск",
    "audit": "Аудит противоречий ТУ и ЗнП",
    "persisting": "Формирование и сохранение замечаний",
    "snapshot": "Сохранение результата в историю",
}


def _run_comparisons(db: Session, job: OperationJob) -> dict:
    pair_ids = job.params["pair_ids"]
    previous = job.result or {}
    results = list(previous.get("runs") or [])
    done = {entry.get("pair_id") for entry in results}
    last_progress = previous.get("comparison_progress")
    for index, pair_id in enumerate(pair_ids):
        if pair_id in done:
            continue
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
            with SessionLocal() as control_db:
                control = control_db.get(OperationJob, job.id)
                if control is None or control.pause_requested:
                    raise ComparisonPause("Пауза после завершённого шага")
                if control.cancel_requested:
                    raise _JobCancelled()
            if stage == "verifying" and clause_number:
                message = f"Анализ положения ЗнП {clause_number}"
            elif stage == "coverage" and clause_number:
                message = f"Покрытие и обратный поиск ТУ {clause_number}"
            elif stage == "audit" and clause_number:
                message = f"Аудит требования ТУ {clause_number}"
            # Independent short transaction: the analysis session may hold a
            # project lock and uncommitted findings. Never commit that session
            # just to publish progress or silently persist partial analysis.
            with SessionLocal() as progress_db:
                _update(progress_db, job.id, stage=stage, message=message,
                        result={"runs": list(results), "comparison_progress": last_progress})

        kwargs = {"progress_callback": report}
        if job.params.get("test_mode", False):
            kwargs["test_mode"] = True
        kwargs["checkpoint_job_id"] = job.id
        kwargs["checkpoint_session_factory"] = SessionLocal
        result = execute_comparison(job.project_id, pair_id, db, **kwargs)
        results.append({"pair_id": pair_id, "run_id": result["run"]["id"]})
        done.add(pair_id)
        _update(db, job.id, stage="comparing",
                message=f"Выполнено сверок: {index + 1} из {len(pair_ids)}",
                current=index + 1, total=len(pair_ids),
                result={"runs": results, "comparison_progress": last_progress})
    return {"runs": results, "comparison_progress": last_progress}


class _JobCancelled(Exception):
    pass


def _run_atom_recheck(db: Session, job: OperationJob) -> dict:
    if job.cancel_requested:
        raise _JobCancelled()
    _update(
        db, job.id, stage="verifying",
        message="Повторная ИИ-проверка выбранного положения ЗнП",
        current=0, total=1,
    )
    previous = db.scalar(select(ComparisonAtomReview).where(
        ComparisonAtomReview.job_id == job.id,
    ))
    if previous is not None:
        return {
            "review_id": previous.id,
            "run_id": previous.run_id,
            "atom_id": previous.atom_id,
        }
    result = run_atom_recheck(
        db, project_id=job.project_id,
        pair_id=job.params["pair_ids"][0],
        run_id=job.params["run_id"],
        atom_id=job.params["atom_id"],
        job_id=job.id,
        session_factory=SessionLocal,
    )
    _update(db, job.id, stage="snapshot", current=1, total=1)
    return result


def recover_pending_operations() -> None:
    """Single-API-instance restart recovery; unfinished provider calls need consent."""
    if ":memory:" in settings.database_url:
        return
    to_start = []
    with SessionLocal() as db:
        rows = db.scalars(select(OperationJob).where(
            OperationJob.kind.in_(COMPARISON_KINDS),
            OperationJob.status.in_(("queued", "running")),
        )).all()
        for job in rows:
            uncertain = db.scalar(select(ComparisonLLMCheckpoint.id).where(
                ComparisonLLMCheckpoint.job_id == job.id,
                ComparisonLLMCheckpoint.status.in_(("sent", "failed")),
            ).limit(1))
            if job.pause_requested or uncertain:
                job.status = "paused"
                job.message = (
                    "Требуется подтверждение повторного запроса после сбоя"
                    if uncertain else "Операция поставлена на паузу"
                )
            else:
                job.status = "queued"
                job.stage = "recovering"
                job.message = "Восстановление после перезапуска сервера"
                to_start.append(job.id)
            job.updated_at = now()
        db.commit()
    for job_id in to_start:
        _workers.submit(_worker, job_id)


def _worker(job_id: str):
    with SessionLocal() as db:
        claimed = db.execute(update(OperationJob).where(
            OperationJob.id == job_id,
            OperationJob.status == "queued",
            OperationJob.pause_requested.is_(False),
        ).values(status="running", stage="starting",
                 message="Подготовка операции", updated_at=now()))
        db.commit()
        if not claimed.rowcount:
            return
        job = db.get(OperationJob, job_id)
        try:
            if job.kind == "upload":
                result = _run_upload(db, job)
            elif job.kind == "normative_extract":
                result = _run_extract(db, job)
            elif job.kind == "normative_check":
                result = _run_check(db, job)
            elif job.kind == "comparison_atom_recheck":
                result = _run_atom_recheck(db, job)
            else:
                result = _run_comparisons(db, job)
            _update(db, job_id, status="completed", stage="complete",
                    message="Операция завершена", result=result)
        except _JobCancelled:
            db.rollback()
            _update(db, job_id, status="cancelled", stage="cancelled",
                    message="Операция остановлена")
        except ComparisonCancelled:
            db.rollback()
            _update(db, job_id, status="cancelled", stage="cancelled",
                    message="Операция остановлена, чекпоинты сохранены")
        except ComparisonPause as exc:
            db.rollback()
            label = str(exc) or "Операция приостановлена"
            _update(db, job_id, status="paused", stage="paused",
                    message="Прогресс сохранён. Можно продолжить.", error=label)
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
                raise HTTPException(409, "Эта сверка уже выполняется или приостановлена")
        if kind in COMPARISON_KINDS:
            for job in db.scalars(select(OperationJob).where(
                OperationJob.project_id == project_id,
                OperationJob.kind.in_(COMPARISON_KINDS),
                OperationJob.kind != kind,
                OperationJob.status.in_(ACTIVE),
            )).all():
                if set(job.params.get("pair_ids", [])) & set(params.get("pair_ids", [])):
                    raise HTTPException(409, "Эта сверка уже выполняется или приостановлена")
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
    if payload.test_mode and payload.kind != "comparison":
        raise HTTPException(422, "Test mode supports one comparison pair only")
    if payload.kind == "comparison_atom_recheck" and (
        not payload.run_id or not payload.atom_id
    ):
        raise HTTPException(422, "Select a saved run and a ZnP atom")
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
        params = {
            "pair_ids": list(dict.fromkeys(payload.pair_ids)),
            "test_mode": payload.test_mode,
        }
        if payload.kind == "comparison_atom_recheck":
            if len(params["pair_ids"]) != 1:
                raise HTTPException(422, "An atom recheck requires exactly one pair")
            run = db.get(ComparisonRun, payload.run_id)
            if run is None or run.pair_id != params["pair_ids"][0]:
                raise HTTPException(404, "Saved comparison run not found")
            rows = ((run.result or {}).get("trace_info") or {}).get("znp_atom_results") or []
            atom = next((x for x in rows if x.get("atom_id") == payload.atom_id), None)
            if atom is None:
                raise HTTPException(404, "ZnP atom not found in saved run")
            if not atom.get("sources"):
                raise HTTPException(
                    409, "Old run does not contain source provenance for safe AI recheck",
                )
            params["run_id"] = payload.run_id
            params["atom_id"] = payload.atom_id
    return _out(_enqueue(db, project_id, payload.kind, params))


@router.get("/projects/{project_id}/operations")
def list_operations(project_id: str, db: Session = Depends(get_db)):
    if db.get(Project, project_id) is None:
        raise HTTPException(404, "Project not found")
    # Paused and active work must never disappear behind 40 newer history rows.
    important = db.scalars(select(OperationJob).where(
        OperationJob.project_id == project_id,
        OperationJob.status.in_(ACTIVE),
    ).order_by(OperationJob.created_at.desc())).all()
    history = db.scalars(select(OperationJob).where(
        OperationJob.project_id == project_id,
        OperationJob.status.not_in(ACTIVE),
    ).order_by(OperationJob.created_at.desc()).limit(40)).all()
    jobs = sorted([*important, *history], key=lambda job: job.created_at, reverse=True)
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
    if job.status == "paused":
        job.status = "cancelled"
        job.pause_requested = False
        job.cancel_requested = True
        job.message = "Операция остановлена, чекпоинты сохранены"
        job.completed_at = now()
        db.commit()
    elif job.status in ACTIVE and not job.cancel_requested:
        job.cancel_requested = True
        job.message = "Остановка после текущего этапа"
        db.commit()
    return _out(job)


@router.post("/projects/{project_id}/operations/{job_id}/pause")
def pause_operation(project_id: str, job_id: str, db: Session = Depends(get_db)):
    _project(db, project_id)
    job = db.get(OperationJob, job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(404, "Operation not found")
    if job.kind not in COMPARISON_KINDS:
        raise HTTPException(422, "Pause is supported for comparisons only")
    if job.status in ("queued", "running"):
        job.pause_requested = True
        if job.status == "queued":
            job.status = "paused"
            job.stage = "paused"
        job.message = "Пауза после текущего запроса; ответы сохранены"
        db.commit()
    return _out(job)


@router.post("/projects/{project_id}/operations/{job_id}/resume")
def resume_operation(project_id: str, job_id: str,
                     payload: ResumeRequest, db: Session = Depends(get_db)):
    _project(db, project_id)
    job = db.get(OperationJob, job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(404, "Operation not found")
    if job.kind not in COMPARISON_KINDS:
        raise HTTPException(422, "Resume is supported for comparisons only")
    if job.status not in ("paused", "failed"):
        raise HTTPException(409, "Operation cannot be resumed from this status")
    uncertain = db.scalar(select(ComparisonLLMCheckpoint.id).where(
        ComparisonLLMCheckpoint.job_id == job.id,
        ComparisonLLMCheckpoint.status.in_(("sent", "failed")),
    ).limit(1))
    if uncertain and not payload.accept_replay_risk:
        raise HTTPException(
            409, "Неизвестно, оплатил ли провайдер последний запрос. "
                 "Подтвердите возможность повторного списания.",
        )
    # Serialize resume with enqueue so a stopped job cannot be reactivated
    # next to another running comparison of the same document pair.
    with _enqueue_lock:
        conflicts = db.scalars(select(OperationJob).where(
            OperationJob.project_id == project_id,
            OperationJob.id != job_id,
            OperationJob.kind.in_(COMPARISON_KINDS),
            OperationJob.status.in_(ACTIVE),
        )).all()
        if any(
            set(other.params.get("pair_ids", [])) &
            set(job.params.get("pair_ids", []))
            for other in conflicts
        ):
            raise HTTPException(409, "Другая операция по этой паре уже выполняется")
        params = dict(job.params)
        params["retry_uncertain"] = bool(payload.accept_replay_risk)
        job.params = params
        job.pause_requested = False
        job.cancel_requested = False
        job.status = "queued"
        job.stage = "queued"
        job.message = "Восстановление из сохранённых ответов DeepSeek"
        job.error = None
        job.completed_at = None
        db.commit()
        _workers.submit(_worker, job.id)
    return _out(job)
