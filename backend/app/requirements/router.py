"""Participant #3 TU/ZnP module: storage, human review and AI-assisted matching.

Manual input and expert confirmations are always available. The automated
comparison endpoint uses an evidence-checked agent and falls back to a
deterministic local pipeline when the LLM is not configured. Results are
persisted as requirements/verifications/findings, and a human verdict remains
the final decision.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.db import get_db
from app.core.models import (
    Chunk,
    Document,
    DocumentKind,
    Finding,
    Fragment,
    Project,
    RequirementCategory as DBRequirementCategory,
    RequirementStatus as DBRequirementStatus,
    RequirementVerification,
    ReviewState,
    ReviewStatus as FindingReviewStatus,
    TURequirement as DBTURequirement,
)
from app.requirements.agent import RequirementsAgent
from app.requirements.schemas import (
    AnalyzeProjectRequirementsRequest,
    CheckRequirementsRequest,
    CheckRequirementsResponse,
    ConfirmRequirementRequest,
    RequirementCategory,
    RequirementCheckItem,
    RequirementStatus,
    RequirementVerificationOut,
    TURequirement,
    TURequirementCreate,
    TURequirementOut,
    ZNPClause,
)
from app.requirements.tools import classify_requirement_category

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
    get_project(db, project_id, edit=True)
    requirement = db.get(DBTURequirement, payload.requirement_id)
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
            status=DBRequirementStatus.needs_review,
            confidence_score=None,
        )
        db.add(verification)
    verification.is_confirmed_by_human = True
    verification.human_override_status = DBRequirementStatus(payload.status.value)
    verification.human_comment = payload.comment

    # The engineer's verdict closes or confirms the related requirement finding.
    prefix = f"[{requirement.clause_number}]"
    findings = db.scalars(
        select(Finding).where(
            Finding.project_id == project_id,
            Finding.module == "requirements",
        )
    ).all()
    for finding in findings:
        if finding.explanation.startswith(prefix):
            finding.review_status = (
                FindingReviewStatus.rejected
                if payload.status == RequirementStatus.fulfilled
                else FindingReviewStatus.accepted
            )

    db.commit()
    db.refresh(verification)
    return verification


def _extract_clauses_from_doc(
    db: Session, doc: Document,
) -> list[tuple[str, str, int, str | None]]:
    """Extract (clause_number, text, page, fragment_id) from chunks or fragments."""
    chunks = db.scalars(
        select(Chunk)
        .where(Chunk.document_id == doc.id)
        .order_by(Chunk.position)
        .options(selectinload(Chunk.sources))
    ).all()
    if chunks:
        result = []
        for idx, chunk in enumerate(chunks, start=1):
            fragment_id = chunk.sources[0].fragment_id if chunk.sources else None
            clause_num = chunk.section_path or f"п. {idx}"
            result.append((clause_num[:32], chunk.text, chunk.page_start, fragment_id))
        return result

    fragments = db.scalars(
        select(Fragment)
        .where(Fragment.document_id == doc.id)
        .order_by(Fragment.page, Fragment.position)
    ).all()
    return [
        ((fragment.section_path or f"п. {idx}")[:32], fragment.text, fragment.page, fragment.id)
        for idx, fragment in enumerate(fragments, start=1)
        if fragment.text and fragment.text.strip()
    ]


@router.post("/analyze", response_model=CheckRequirementsResponse)
def analyze_requirements(
    payload: AnalyzeProjectRequirementsRequest | CheckRequirementsRequest,
    db: Session = Depends(get_db),
):
    """Run the agent-based TU/ZnP comparison.

    Two scenarios are supported:
    - explicit lists of TU requirements and ZnP clauses (tests, benchmarks);
    - a stored project with TU and ZnP documents (chunks are extracted from DB).
    """
    agent = RequirementsAgent()

    if isinstance(payload, CheckRequirementsRequest):
        return agent.verify_all(payload.tu_requirements, payload.znp_clauses)

    project_id = payload.project_id
    project = get_project(db, project_id, edit=True)

    docs = db.scalars(select(Document).where(Document.project_id == project_id)).all()
    docs_by_id = {document.id: document for document in docs}

    if payload.tu_document_id:
        tu_doc = docs_by_id.get(payload.tu_document_id)
        if not tu_doc:
            raise HTTPException(404, "TU document not found in this project")
    else:
        tu_doc = next((document for document in docs if document.kind == DocumentKind.tu), None)

    if payload.znp_document_id:
        znp_doc = docs_by_id.get(payload.znp_document_id)
        if not znp_doc:
            raise HTTPException(404, "ZNP document not found in this project")
    else:
        znp_doc = next((document for document in docs if document.kind == DocumentKind.znp), None)

    if not tu_doc or not znp_doc:
        raise HTTPException(422, "Project must contain both TU (tu) and ZNP (znp) documents")

    tu_raw = _extract_clauses_from_doc(db, tu_doc)
    znp_raw = _extract_clauses_from_doc(db, znp_doc)

    # Re-running the analysis replaces previous automatic results only.
    for old_verification in db.scalars(
        select(RequirementVerification).where(RequirementVerification.project_id == project_id)
    ).all():
        db.delete(old_verification)
    for old_requirement in db.scalars(
        select(DBTURequirement).where(DBTURequirement.project_id == project_id)
    ).all():
        db.delete(old_requirement)
    for old_finding in db.scalars(
        select(Finding).where(
            Finding.project_id == project_id,
            Finding.module == "requirements",
        )
    ).all():
        db.delete(old_finding)
    db.flush()

    tu_schemas: list[TURequirement] = []
    tu_meta: dict[str, tuple[int, str | None]] = {}
    for clause_num, text_value, page, fragment_id in tu_raw:
        category = classify_requirement_category(text_value)
        db_requirement = DBTURequirement(
            project_id=project_id,
            document_id=tu_doc.id,
            clause_number=clause_num,
            category=DBRequirementCategory(category.value),
            text=text_value,
        )
        db.add(db_requirement)
        db.flush()
        tu_schemas.append(
            TURequirement(
                id=db_requirement.id,
                clause_number=clause_num,
                category=category,
                text=text_value,
            )
        )
        tu_meta[db_requirement.id] = (page, fragment_id)

    znp_schemas: list[ZNPClause] = []
    znp_meta: dict[str, tuple[int, str | None]] = {}
    for idx, (clause_num, text_value, page, fragment_id) in enumerate(znp_raw, start=1):
        clause_id = f"znp-{idx}"
        znp_schemas.append(ZNPClause(id=clause_id, clause_number=clause_num, text=text_value))
        znp_meta[clause_id] = (page, fragment_id)

    response = agent.verify_all(tu_schemas, znp_schemas)

    for item in response.checked_requirements:
        db.add(
            RequirementVerification(
                id=item.id,
                project_id=project_id,
                requirement_id=item.requirement.id,
                status=DBRequirementStatus(item.status.value),
                confidence_score=item.confidence_score,
                matched_znp_clause_number=(
                    item.matched_clause.clause_number if item.matched_clause else None
                ),
                matched_znp_text=item.matched_clause.text if item.matched_clause else None,
                tu_quote=item.tu_quote,
                znp_quote=item.znp_quote,
                reasoning=item.reasoning,
                is_confirmed_by_human=False,
            )
        )

        if item.status in (RequirementStatus.missing, RequirementStatus.needs_review):
            tu_page, tu_fragment_id = tu_meta.get(item.requirement.id, (1, None))
            evidence_list = [
                {
                    "document_id": tu_doc.id,
                    "fragment_id": tu_fragment_id,
                    "page": tu_page,
                    "quote": item.tu_quote or item.requirement.text[:200],
                }
            ]
            if item.matched_clause and item.znp_quote:
                znp_page, znp_fragment_id = znp_meta.get(item.matched_clause.id, (1, None))
                evidence_list.append({
                    "document_id": znp_doc.id,
                    "fragment_id": znp_fragment_id,
                    "page": znp_page,
                    "quote": item.znp_quote,
                })

            db.add(
                Finding(
                    project_id=project_id,
                    module="requirements",
                    type=(
                        "missing_requirement"
                        if item.status == RequirementStatus.missing
                        else "needs_review"
                    ),
                    severity=(
                        "error" if item.status == RequirementStatus.missing else "warning"
                    ),
                    explanation=f"[{item.requirement.clause_number}] {item.reasoning}",
                    suggested_action=(
                        "Дополнить ЗнП пропущенным требованием ТУ"
                        if item.status == RequirementStatus.missing
                        else "Проверить соответствие параметров ТУ и ЗнП инженером"
                    ),
                    evidence=evidence_list,
                )
            )

    db.commit()
    return response


@router.get("/projects/{project_id}", response_model=CheckRequirementsResponse)
def get_project_requirements(project_id: str, db: Session = Depends(get_db)):
    """Return persisted TU/ZnP comparison results for a project."""
    get_project(db, project_id)

    requirements = {
        requirement.id: requirement
        for requirement in db.scalars(
            select(DBTURequirement).where(DBTURequirement.project_id == project_id)
        ).all()
    }
    verifications = db.scalars(
        select(RequirementVerification)
        .where(RequirementVerification.project_id == project_id)
        .order_by(RequirementVerification.created_at)
    ).all()

    items: list[RequirementCheckItem] = []
    for verification in verifications:
        requirement = requirements.get(verification.requirement_id)
        if not requirement:
            continue
        effective_status = RequirementStatus(
            (verification.human_override_status or verification.status).value
        )
        matched = (
            ZNPClause(
                id=f"znp-{verification.id[:8]}",
                clause_number=verification.matched_znp_clause_number or "—",
                text=verification.matched_znp_text or "",
            )
            if verification.matched_znp_text
            else None
        )
        items.append(
            RequirementCheckItem(
                id=verification.id,
                requirement=TURequirement(
                    id=requirement.id,
                    clause_number=requirement.clause_number,
                    category=RequirementCategory(requirement.category.value),
                    text=requirement.text,
                ),
                status=effective_status,
                confidence_score=verification.confidence_score or 0.0,
                matched_clause=matched,
                tu_quote=verification.tu_quote,
                znp_quote=verification.znp_quote,
                reasoning=verification.reasoning,
                is_confirmed_by_human=verification.is_confirmed_by_human,
                human_comment=verification.human_comment,
                human_override_status=(
                    RequirementStatus(verification.human_override_status.value)
                    if verification.human_override_status
                    else None
                ),
            )
        )

    stats = {
        "fulfilled": sum(1 for item in items if item.status == RequirementStatus.fulfilled),
        "missing": sum(1 for item in items if item.status == RequirementStatus.missing),
        "needs_review": sum(1 for item in items if item.status == RequirementStatus.needs_review),
        "total": len(items),
    }
    return CheckRequirementsResponse(checked_requirements=items, stats=stats, trace_info=None)
