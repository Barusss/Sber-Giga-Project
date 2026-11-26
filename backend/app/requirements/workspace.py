"""Project-scoped comparison plans, preserved runs and controlled deletion.

The existing engineering analysis stays the single source of truth for
calculation. This module manages the requested pairs and snapshots each run.
"""
from copy import deepcopy
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.db import get_db
from app.documents.parser import TABLE_REVIEW, requires_pdf_layout_review
from app.core.models import (
    ComparisonPair, ComparisonRun, ComparisonAtomReview, Document, DocumentKind, Fragment, Finding, Project,
    RequirementAnalysis, RequirementVerification, TURequirement, ReviewState,
)
from app.requirements.analysis import analyze_project_requirements
from app.requirements.storage import (
    analysis_lock, confirm_requirement, finding_in_pair,
    get_project, lock_project_for_analysis, load_project_requirements,
)
from app.requirements.znp_first import ZnPFirstAgent
from app.requirements.atomization import atomize_fragments, atomize_document
from app.requirements.source_coverage import (
    source_coverage_ledger, source_coverage_summary,
)
from app.requirements.checkpoints import CheckpointedDeepSeek
from app.ai.deepseek import DeepSeekClient
from app.requirements.schemas import (
    AnalyzeProjectRequirementsRequest, ConfirmRequirementRequest, RequirementStatus,
)

router = APIRouter(tags=["comparisons"])


class PairInput(BaseModel):
    tu_document_id: str
    znp_document_id: str


class PairBatch(BaseModel):
    pairs: list[PairInput] = Field(min_length=1, max_length=100)


class RunConfirm(BaseModel):
    verification_id: str
    status: RequirementStatus
    comment: str | None = None


def _pair(db: Session, project_id: str, pair_id: str, edit: bool = False) -> ComparisonPair:
    get_project(db, project_id, edit=edit)
    pair = db.get(ComparisonPair, pair_id)
    if pair is None or pair.project_id != project_id:
        raise HTTPException(404, "Comparison not found")
    return pair


def _run(db: Session, pair: ComparisonPair, run_id: str) -> ComparisonRun:
    run = db.get(ComparisonRun, run_id)
    if run is None or run.pair_id != pair.id:
        raise HTTPException(404, "Run not found for this comparison")
    return run


def _summary(run: ComparisonRun) -> dict:
    result = run.result or {}
    items = result.get("checked_requirements") or []
    stats = result.get("stats") or {}
    return {
        "id": run.id,
        "created_at": run.created_at,
        "is_current": run.is_current,
        "stats": stats,
        "test_mode": bool((result.get("trace_info") or {}).get("test_mode", {}).get("enabled")),
        "confirmed": sum(bool(item.get("is_confirmed_by_human")) for item in items),
    }


def _serialize(pair: ComparisonPair, db: Session) -> dict:
    runs = sorted(pair.runs, key=lambda r: (r.created_at, r.id), reverse=True)
    current = next((r for r in runs if r.is_current), None)
    latest = _summary(current) if current else None
    confirmed = latest["confirmed"] if latest else 0
    total = (latest["stats"].get("total") or 0) if latest else 0
    state = "queued" if current is None else "reviewed" if total and confirmed == total else "ready"
    tu = db.get(Document, pair.tu_document_id)
    znp = db.get(Document, pair.znp_document_id)
    return {
        "id": pair.id,
        "project_id": pair.project_id,
        "tu_document_id": pair.tu_document_id,
        "znp_document_id": pair.znp_document_id,
        "tu_filename": tu.filename if tu else "",
        "znp_filename": znp.filename if znp else "",
        "created_at": pair.created_at,
        "status": state,
        "run_count": len(runs),
        "current_run_id": current.id if current else None,
        "latest": latest,
    }


def _legacy_pairs(db: Session, project_id: str) -> None:
    """Expose older completed work in the new UI without losing its results.

    Backfilling creates a single historical snapshot for each older pair.
    Re-running the list endpoint is idempotent.
    """
    docs = db.scalars(select(Document).where(Document.project_id == project_id)).all()
    ids = {doc.id for doc in docs}
    legacy = db.scalars(select(RequirementAnalysis).where(
        RequirementAnalysis.tu_document_id.in_(ids),
        RequirementAnalysis.znp_document_id.in_(ids),
    )).all()
    if not legacy:
        return
    existing = {
        (pair.tu_document_id, pair.znp_document_id): pair
        for pair in db.scalars(select(ComparisonPair).where(
            ComparisonPair.project_id == project_id,
        )).all()
    }
    changed = False
    for row in legacy:
        key = (row.tu_document_id, row.znp_document_id)
        if key in existing:
            continue
        pair = ComparisonPair(
            project_id=project_id, tu_document_id=key[0], znp_document_id=key[1],
            created_at=row.completed_at or datetime.now(timezone.utc),
        )
        db.add(pair)
        db.flush()
        saved = load_project_requirements(db, project_id, key[0], key[1])
        result = saved.model_dump(mode="json")
        db.add(ComparisonRun(
            pair_id=pair.id, result=result, is_current=True,
            created_at=row.completed_at or datetime.now(timezone.utc),
        ))
        existing[key] = pair
        changed = True
    if changed:
        db.commit()


@router.get("/projects/{project_id}/comparisons")
def list_comparisons(project_id: str, db: Session = Depends(get_db)):
    get_project(db, project_id)
    with analysis_lock(project_id):
        _legacy_pairs(db, project_id)
    pairs = db.scalars(
        select(ComparisonPair).where(ComparisonPair.project_id == project_id)
        .options(selectinload(ComparisonPair.runs))
        .order_by(ComparisonPair.created_at, ComparisonPair.id)
    ).all()
    return [_serialize(pair, db) for pair in pairs]


@router.post("/projects/{project_id}/comparisons", status_code=201)
def create_comparisons(project_id: str, payload: PairBatch, db: Session = Depends(get_db)):
    get_project(db, project_id, edit=True)
    docs = db.scalars(select(Document).where(Document.project_id == project_id)).all()
    by_id = {doc.id: doc for doc in docs}
    for entry in payload.pairs:
        tu, znp = by_id.get(entry.tu_document_id), by_id.get(entry.znp_document_id)
        if tu is None or znp is None or tu.kind != DocumentKind.tu or znp.kind != DocumentKind.znp:
            raise HTTPException(422, "Each pair must contain TU and ZNP documents from this project")
    with analysis_lock(project_id):
        _legacy_pairs(db, project_id)
        existing = {
            (p.tu_document_id, p.znp_document_id)
            for p in db.scalars(select(ComparisonPair).where(
                ComparisonPair.project_id == project_id
            )).all()
        }
        for entry in payload.pairs:
            key = (entry.tu_document_id, entry.znp_document_id)
            if key not in existing:
                db.add(ComparisonPair(project_id=project_id, tu_document_id=key[0], znp_document_id=key[1]))
                existing.add(key)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Comparison was created concurrently")
    return list_comparisons(project_id, db)


@router.get("/projects/{project_id}/comparisons/{pair_id}")
def get_comparison(project_id: str, pair_id: str, db: Session = Depends(get_db)):
    pair = _pair(db, project_id, pair_id)
    db.refresh(pair, attribute_names=["runs"])
    return _serialize(pair, db)


@router.post("/projects/{project_id}/comparisons/{pair_id}/runs", status_code=201)
def execute_comparison(project_id: str, pair_id: str, test_mode: bool = False,
                       allow_incomplete_sources: bool = False,
                       preview_znp_source_id: str | None = None,
                       db: Session = Depends(get_db)):
    return run_comparison(
        project_id, pair_id, db, test_mode=test_mode,
        allow_incomplete_sources=allow_incomplete_sources,
        preview_znp_source_id=preview_znp_source_id,
    )


def run_comparison(
    project_id: str, pair_id: str, db: Session,
    progress_callback: Callable[[str, int, int, str | None], None] | None = None,
    *,
    test_mode: bool = False,
    allow_incomplete_sources: bool = False,
    preview_znp_source_id: str | None = None,
    checkpoint_job_id: str | None = None,
    checkpoint_session_factory=None,
):
    pair = _pair(db, project_id, pair_id, edit=True)
    if checkpoint_job_id:
        finished = db.scalar(select(ComparisonRun).where(
            ComparisonRun.pair_id == pair.id,
            ComparisonRun.operation_job_id == checkpoint_job_id,
        ))
        if finished is not None:
            return {"run": _summary(finished), "result": finished.result}
    model = DeepSeekClient()
    if checkpoint_job_id:
        model = CheckpointedDeepSeek(
            checkpoint_job_id, model, session_factory=checkpoint_session_factory,
        )
    agent = ZnPFirstAgent(ai_client=model)
    published_run: ComparisonRun | None = None

    def publish(response, session):
        nonlocal published_run
        if progress_callback:
            # Pause check BEFORE the atomic official-result transaction commits.
            progress_callback(
                "snapshot", response.stats["total"], response.stats["total"], None,
            )
        for old in session.scalars(select(ComparisonRun).where(
            ComparisonRun.pair_id == pair.id,
            ComparisonRun.is_current.is_(True),
        )).all():
            old.is_current = False
        published_run = ComparisonRun(
            pair_id=pair.id, operation_job_id=checkpoint_job_id,
            result=response.model_dump(mode="json"), is_current=True,
        )
        session.add(published_run)
        session.flush()

    try:
        with analysis_lock(project_id):
            lock_project_for_analysis(db, project_id)
            response = analyze_project_requirements(
                AnalyzeProjectRequirementsRequest(
                    project_id=project_id,
                    tu_document_id=pair.tu_document_id,
                    znp_document_id=pair.znp_document_id,
                    force=False,
                    test_mode=test_mode,
                    preview_znp_source_id=preview_znp_source_id,
                    allow_incomplete_sources=allow_incomplete_sources,
                ), db, agent, progress_callback=progress_callback,
                on_published=None if test_mode else publish,
            )
            if test_mode and progress_callback:
                progress_callback("snapshot", response.stats["total"], response.stats["total"], None)
            if test_mode:
                # Preview remains non-current and does not change findings.
                run = ComparisonRun(
                    pair_id=pair.id, operation_job_id=checkpoint_job_id,
                    result=response.model_dump(mode="json"), is_current=False,
                )
                db.add(run)
                db.commit()
            else:
                # Committed in the SAME transaction as comparison verifications.
                run = published_run
                if run is None:
                    raise RuntimeError("Comparison published without snapshot")
            db.refresh(run)
            return {"run": _summary(run), "result": run.result}
    finally:
        if hasattr(agent.ai, "close"):
            agent.ai.close()



@router.get("/projects/{project_id}/comparisons/{pair_id}/source-coverage")
def get_source_coverage(project_id: str, pair_id: str,
                        db: Session = Depends(get_db)):
    """No-API source completeness preflight before a paid full comparison."""
    pair = _pair(db, project_id, pair_id)
    documents = db.scalars(select(Document).where(Document.id.in_(
        [pair.tu_document_id, pair.znp_document_id]
    ))).all()
    review_ids = [doc.id for doc in documents if requires_pdf_layout_review(doc)]
    fragments = db.scalars(select(Fragment).where(
        Fragment.document_id == pair.znp_document_id,
    ).order_by(Fragment.page, Fragment.position)).all()
    if not fragments:
        return {
            "available": False, "complete": False, TABLE_REVIEW: review_ids,
            "unclassified_count": 0,
            "message": "Документ не имеет индексированных исходных фрагментов.",
        }
    znp_document = next(doc for doc in documents if doc.id == pair.znp_document_id)
    ledger = source_coverage_ledger(
        fragments, atomize_document(znp_document, fragments),
    )
    return {"available": True, **source_coverage_summary(ledger),
            "complete": ledger["complete"] and not review_ids, TABLE_REVIEW: review_ids}


@router.get("/projects/{project_id}/comparisons/{pair_id}/runs")
def list_runs(project_id: str, pair_id: str, db: Session = Depends(get_db)):
    pair = _pair(db, project_id, pair_id)
    runs = db.scalars(select(ComparisonRun).where(ComparisonRun.pair_id == pair.id)
        .order_by(ComparisonRun.created_at.desc(), ComparisonRun.id.desc())).all()
    return [_summary(run) for run in runs]


@router.get("/projects/{project_id}/comparisons/{pair_id}/runs/{run_id}")
def get_run(project_id: str, pair_id: str, run_id: str, db: Session = Depends(get_db)):
    pair = _pair(db, project_id, pair_id)
    run = _run(db, pair, run_id)
    return {"run": _summary(run), "result": run.result}


@router.get("/projects/{project_id}/comparisons/{pair_id}/runs/{run_id}/atom-reviews")
def list_atom_reviews(project_id: str, pair_id: str, run_id: str,
                      atom_id: str | None = None, db: Session = Depends(get_db)):
    pair = _pair(db, project_id, pair_id)
    _run(db, pair, run_id)
    rows = db.scalars(select(ComparisonAtomReview).where(
        ComparisonAtomReview.run_id == run_id,
    ).order_by(ComparisonAtomReview.created_at.desc())).all()
    if atom_id is not None:
        rows = [row for row in rows if row.atom_id == atom_id]
    return [{
        "id": row.id, "atom_id": row.atom_id,
        "created_at": row.created_at, "result": row.result,
    } for row in rows]


@router.post("/projects/{project_id}/comparisons/{pair_id}/runs/{run_id}/confirm")
def confirm_run_item(project_id: str, pair_id: str, run_id: str,
                     payload: RunConfirm, db: Session = Depends(get_db)):
    pair = _pair(db, project_id, pair_id, edit=True)
    run = _run(db, pair, run_id)
    if not run.is_current:
        raise HTTPException(409, "Historical runs are read-only")
    items = (run.result or {}).get("checked_requirements", [])
    item = next((v for v in items if v.get("id") == payload.verification_id), None)
    if item is None:
        raise HTTPException(404, "Verification not found in this run")
    with analysis_lock(project_id):
        lock_project_for_analysis(db, project_id)
        verification = confirm_requirement(project_id, ConfirmRequirementRequest(
            verification_id=payload.verification_id,
            requirement_id=item["requirement"]["id"],
            znp_document_id=pair.znp_document_id,
            status=payload.status,
            comment=payload.comment,
        ), db)
        stored = deepcopy(run.result)
        for entry in stored["checked_requirements"]:
            if entry["id"] == verification.id:
                entry["status"] = payload.status.value
                entry["is_confirmed_by_human"] = True
                entry["human_override_status"] = payload.status.value
                entry["human_comment"] = payload.comment
        stats = {key: sum(v["status"] == key for v in stored["checked_requirements"])
                 for key in ("fulfilled", "missing", "needs_review")}
        stored["stats"] = {**stats, "total": len(stored["checked_requirements"])}
        run.result = stored
        db.commit()
    return {"run": _summary(run), "result": run.result}


@router.delete("/projects/{project_id}/comparisons/{pair_id}/runs/{run_id}", status_code=204)
def delete_run(project_id: str, pair_id: str, run_id: str, db: Session = Depends(get_db)):
    with analysis_lock(project_id):
        pair = _pair(db, project_id, pair_id, edit=True)
        db.delete(_run(db, pair, run_id))
        db.commit()


@router.delete("/projects/{project_id}/comparisons/{pair_id}", status_code=204)
def delete_comparison(project_id: str, pair_id: str, db: Session = Depends(get_db)):
    with analysis_lock(project_id):
        pair = _pair(db, project_id, pair_id, edit=True)
        tu_reqs = db.scalars(select(TURequirement).where(
            TURequirement.project_id == project_id,
            TURequirement.document_id == pair.tu_document_id,
        )).all()
        req_ids = [r.id for r in tu_reqs]
        if req_ids:
            db.execute(delete(RequirementVerification).where(
                RequirementVerification.requirement_id.in_(req_ids),
                RequirementVerification.znp_document_id == pair.znp_document_id,
            ))
        for finding in db.scalars(select(Finding).where(
            Finding.project_id == project_id, Finding.module == "requirements",
        )).all():
            if finding_in_pair(finding, pair.tu_document_id, pair.znp_document_id):
                db.delete(finding)
        analysis = db.get(RequirementAnalysis, (pair.tu_document_id, pair.znp_document_id))
        if analysis:
            db.delete(analysis)
        db.delete(pair)
        db.commit()
