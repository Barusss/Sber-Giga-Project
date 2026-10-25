"""Participant #3 TU/ZnP module: storage, human review and AI-assisted matching.

Manual input and expert confirmations are always available. The automated
comparison endpoint uses an evidence-checked agent and falls back to a
deterministic local pipeline when the LLM is not configured. Results are
persisted as requirements/verifications/findings, and a human verdict remains
the final decision.
"""
import threading
from datetime import timezone

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
from app.requirements.extraction import (
    Clause,
    extract_clauses,
    is_service_clause,
    normalize_clause_text,
)
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


def _finding_requirement_id(finding: Finding) -> str | None:
    for entry in finding.evidence or []:
        if isinstance(entry, dict) and entry.get("requirement_id"):
            return str(entry["requirement_id"])
    return None


def _finding_matches(finding: Finding, requirement: DBTURequirement) -> bool:
    """Prefer the stable evidence link; keep the old clause prefix for legacy rows."""
    linked_id = _finding_requirement_id(finding)
    if linked_id is not None:
        return linked_id == requirement.id
    return finding.explanation.startswith(f"[{requirement.clause_number}]")


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
    findings = db.scalars(
        select(Finding).where(
            Finding.project_id == project_id,
            Finding.module == "requirements",
        )
    ).all()
    for finding in findings:
        if _finding_matches(finding, requirement):
            finding.review_status = (
                FindingReviewStatus.rejected
                if payload.status == RequirementStatus.fulfilled
                else FindingReviewStatus.accepted
            )

    db.commit()
    db.refresh(verification)
    return verification


def _extract_clauses_from_doc(db: Session, doc: Document) -> list[Clause]:
    """Extract atomic clauses from fragments; chunks are only a legacy fallback."""
    fragments = db.scalars(
        select(Fragment)
        .where(Fragment.document_id == doc.id)
        .order_by(Fragment.page, Fragment.position)
    ).all()
    if fragments:
        return extract_clauses(fragments)

    chunks = db.scalars(
        select(Chunk)
        .where(Chunk.document_id == doc.id)
        .order_by(Chunk.position)
        .options(selectinload(Chunk.sources))
    ).all()
    clauses: list[Clause] = []
    for idx, chunk in enumerate(chunks, start=1):
        fragment_id = chunk.sources[0].fragment_id if chunk.sources else None
        clause_number = (chunk.section_path or f"п. {idx}")[:32]
        clauses.append(
            Clause(
                clause_number=clause_number,
                text=chunk.text,
                page=chunk.page_start,
                fragment_id=fragment_id,
                fragment_ids=[fragment_id] if fragment_id else [],
                section_path=chunk.section_path,
                fragment_text=chunk.text,
            )
        )
    return clauses


def _requirement_key(document_id: str, clause_number: str, text: str) -> tuple[str, str, str]:
    return (document_id, clause_number, normalize_clause_text(text))


# Analyses are serialized per project. PostgreSQL also locks the project row,
# which protects deployments with several API workers.
_ANALYSIS_LOCKS: dict[str, threading.Lock] = {}
_ANALYSIS_LOCKS_GUARD = threading.Lock()


def _analysis_lock(project_id: str) -> threading.Lock:
    with _ANALYSIS_LOCKS_GUARD:
        return _ANALYSIS_LOCKS.setdefault(project_id, threading.Lock())


def _lock_project_for_analysis(db: Session, project_id: str) -> Project:
    if db.get_bind().dialect.name == "postgresql":
        project = db.scalar(
            select(Project).where(Project.id == project_id).with_for_update()
        )
        if project is None:
            raise HTTPException(404, "Project not found")
        if project.state == ReviewState.approved:
            raise HTTPException(409, "Approved project cannot be modified")
        return project

    # SQLite has no row-level locks; the in-process lock above is the guard.
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    db.refresh(project)
    if project.state == ReviewState.approved:
        raise HTTPException(409, "Approved project cannot be modified")
    return project


def _timestamp(value) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _accepted_requirement_ids(
    findings: list[Finding], requirements: list[DBTURequirement],
) -> set[str]:
    accepted: set[str] = set()
    for finding in findings:
        if finding.review_status != FindingReviewStatus.accepted:
            continue
        linked_id = _finding_requirement_id(finding)
        if linked_id is not None:
            accepted.add(linked_id)
        else:
            accepted.update(
                requirement.id
                for requirement in requirements
                if _finding_matches(finding, requirement)
            )
    return accepted


def _verification_rank(
    verification: RequirementVerification, accepted_ids: set[str],
):
    """Human verdict first, then an accepted finding, then the freshest record."""
    human = bool(
        verification.is_confirmed_by_human
        or verification.human_override_status is not None
    )
    accepted = verification.requirement_id in accepted_ids
    return (
        1 if human else 0,
        1 if accepted else 0,
        _timestamp(verification.updated_at or verification.created_at),
        verification.id,
    )


def _best_verification(
    verifications: list[RequirementVerification], accepted_ids: set[str],
) -> RequirementVerification | None:
    return max(
        verifications,
        key=lambda verification: _verification_rank(verification, accepted_ids),
        default=None,
    )


def _deduplicate_requirements(
    db: Session,
    requirements: list[DBTURequirement],
    verifications: list[RequirementVerification],
    findings: list[Finding],
) -> int:
    """Merge exact stable-key duplicates without losing human/finding decisions.

    The surviving requirement keeps one best verification (human verdicts win);
    other verifications and duplicate requirements are removed, and finding
    evidence is re-pointed to the survivor. Manual rows are preferred as the
    survivor, so a manually prepared requirement is not discarded.
    """
    accepted_ids = _accepted_requirement_ids(findings, requirements)
    by_requirement: dict[str, list[RequirementVerification]] = {}
    for verification in verifications:
        by_requirement.setdefault(verification.requirement_id, []).append(verification)

    groups: dict[tuple[str, str, str], list[DBTURequirement]] = {}
    for requirement in requirements:
        groups.setdefault(
            _requirement_key(
                requirement.document_id, requirement.clause_number, requirement.text,
            ),
            [],
        ).append(requirement)

    removed = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        group_ids = {requirement.id for requirement in group}
        group_verifications = [
            verification
            for requirement in group
            for verification in by_requirement.get(requirement.id, [])
        ]
        keeper = _best_verification(group_verifications, accepted_ids)

        def survivor_rank(requirement: DBTURequirement):
            best = _best_verification(
                by_requirement.get(requirement.id, []), accepted_ids,
            )
            best_rank = (
                _verification_rank(best, accepted_ids) if best else (0, 0, 0.0, "")
            )
            manual = 1 if requirement.fragment_id is None else 0
            return (
                manual,
                best_rank[0],
                best_rank[1],
                -_timestamp(requirement.created_at),
                requirement.id,
            )

        survivor = max(group, key=survivor_rank)
        if keeper is not None:
            keeper.requirement_id = survivor.id
        for verification in group_verifications:
            if verification is not keeper:
                db.delete(verification)

        for finding in findings:
            evidence = []
            changed = False
            for entry in finding.evidence or []:
                if (
                    isinstance(entry, dict)
                    and entry.get("requirement_id") in group_ids
                ):
                    entry = {**entry, "requirement_id": survivor.id}
                    changed = True
                evidence.append(entry)
            if changed:
                finding.evidence = evidence

        for requirement in group:
            if requirement is not survivor:
                db.delete(requirement)
                removed += 1
    return removed


def _attach_provenance(
    items: list[RequirementCheckItem],
    tu_requirements: list[TURequirement],
    znp_clauses: list[ZNPClause],
) -> None:
    """Copy fragment provenance from source clauses into check items."""
    tu_by_id = {requirement.id: requirement for requirement in tu_requirements}
    znp_by_id = {clause.id: clause for clause in znp_clauses}
    for item in items:
        tu_clause = tu_by_id.get(item.requirement.id)
        if tu_clause is not None:
            item.tu_fragment_id = tu_clause.fragment_id
            item.tu_page = tu_clause.page
            item.tu_fragment_text = tu_clause.fragment_text
        if item.matched_clause is not None:
            znp_clause = znp_by_id.get(item.matched_clause.id)
            if znp_clause is not None:
                item.znp_fragment_id = znp_clause.fragment_id
                item.znp_page = znp_clause.page
                item.znp_fragment_text = znp_clause.fragment_text


def _normalize_ws(value: str) -> str:
    return " ".join(value.split()).casefold()


def _fragment_for_quote(
    quote: str | None,
    clause: Clause | None,
    fragments_by_id: dict[str, Fragment],
) -> Fragment | None:
    """Find the fragment that really contains the quote; otherwise use the first one."""
    if clause is None:
        return None
    candidates = [
        fragments_by_id[fragment_id]
        for fragment_id in clause.fragment_ids
        if fragment_id in fragments_by_id
    ]
    if not candidates:
        return None
    needle = _normalize_ws(quote) if quote else ""
    if needle:
        for fragment in candidates:
            if needle in _normalize_ws(fragment.text):
                return fragment
    return candidates[0]


def _shorten_to_fragment(quote: str | None, fragment: Fragment | None) -> str | None:
    if not quote or fragment is None:
        return quote
    if len(quote) <= len(fragment.text):
        return quote
    shortened = quote[:len(fragment.text)]
    return shortened.rsplit(" ", 1)[0] or shortened


def _apply_quote_provenance(
    items: list[RequirementCheckItem],
    tu_clauses: dict[str, Clause],
    znp_clauses: dict[str, Clause],
    fragments_by_id: dict[str, Fragment],
) -> None:
    for item in items:
        tu_clause = tu_clauses.get(item.requirement.id)
        tu_fragment = _fragment_for_quote(item.tu_quote, tu_clause, fragments_by_id)
        if tu_fragment is not None:
            item.tu_fragment_id = tu_fragment.id
            item.tu_page = tu_fragment.page
            item.tu_fragment_text = tu_fragment.text
            item.tu_quote = _shorten_to_fragment(item.tu_quote, tu_fragment)

        if item.matched_clause is None:
            continue
        znp_clause = znp_clauses.get(item.matched_clause.id)
        znp_fragment = _fragment_for_quote(item.znp_quote, znp_clause, fragments_by_id)
        if znp_fragment is not None:
            item.znp_fragment_id = znp_fragment.id
            item.znp_page = znp_fragment.page
            item.znp_fragment_text = znp_fragment.text
            item.znp_quote = _shorten_to_fragment(item.znp_quote, znp_fragment)


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
    agent = RequirementsAgent()

    if isinstance(payload, CheckRequirementsRequest):
        response = agent.verify_all(payload.tu_requirements, payload.znp_clauses)
        _attach_provenance(
            response.checked_requirements,
            payload.tu_requirements,
            payload.znp_clauses,
        )
        return response

    project_id = payload.project_id

    with _analysis_lock(project_id):
        _lock_project_for_analysis(db, project_id)
        return _analyze_project_requirements(payload, db, agent)


def _analyze_project_requirements(
    payload: AnalyzeProjectRequirementsRequest,
    db: Session,
    agent: RequirementsAgent,
) -> CheckRequirementsResponse:
    project_id = payload.project_id

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

    tu_raw = [
        clause
        for clause in _extract_clauses_from_doc(db, tu_doc)
        if not is_service_clause(clause.text)
    ]
    znp_raw = _extract_clauses_from_doc(db, znp_doc)

    fragment_ids = {
        fragment_id
        for clause in [*tu_raw, *znp_raw]
        for fragment_id in clause.fragment_ids
        if fragment_id
    }
    fragments_by_id = {
        fragment.id: fragment
        for fragment in (
            db.scalars(select(Fragment).where(Fragment.id.in_(fragment_ids))).all()
            if fragment_ids else []
        )
    }

    existing_requirements = db.scalars(
        select(DBTURequirement).where(DBTURequirement.project_id == project_id)
    ).all()
    existing_verifications = db.scalars(
        select(RequirementVerification)
        .where(RequirementVerification.project_id == project_id)
    ).all()
    existing_findings = db.scalars(
        select(Finding).where(
            Finding.project_id == project_id,
            Finding.module == "requirements",
        )
    ).all()

    # Legacy or parallel runs may have left exact duplicates. Merge them before
    # matching so the next analysis does not keep both copies.
    deduplicated = _deduplicate_requirements(
        db, existing_requirements, existing_verifications, existing_findings,
    )
    if deduplicated:
        db.flush()
        existing_requirements = db.scalars(
            select(DBTURequirement).where(DBTURequirement.project_id == project_id)
        ).all()
        existing_verifications = db.scalars(
            select(RequirementVerification)
            .where(RequirementVerification.project_id == project_id)
        ).all()
        existing_findings = db.scalars(
            select(Finding).where(
                Finding.project_id == project_id,
                Finding.module == "requirements",
            )
        ).all()

    existing_by_key = {
        _requirement_key(
            requirement.document_id, requirement.clause_number, requirement.text,
        ): requirement
        for requirement in existing_requirements
    }
    accepted_ids = _accepted_requirement_ids(existing_findings, existing_requirements)
    verifications_by_requirement: dict[str, list[RequirementVerification]] = {}
    for verification in existing_verifications:
        verifications_by_requirement.setdefault(
            verification.requirement_id, [],
        ).append(verification)
    verification_by_requirement: dict[str, RequirementVerification] = {}
    for requirement_id, items in verifications_by_requirement.items():
        best = _best_verification(items, accepted_ids)
        if best is not None:
            verification_by_requirement[requirement_id] = best

    analysis = {
        "created": 0,
        "updated": 0,
        "preserved_human": 0,
        "preserved_findings": 0,
        "removed": deduplicated,
        "force": payload.force,
    }

    tu_schemas: list[TURequirement] = []
    tu_clauses: dict[str, Clause] = {}
    db_requirements: dict[str, DBTURequirement] = {}
    for clause in tu_raw:
        category = classify_requirement_category(clause.text)
        key = _requirement_key(tu_doc.id, clause.clause_number, clause.text)
        db_requirement = existing_by_key.get(key)
        if db_requirement is None:
            db_requirement = DBTURequirement(
                project_id=project_id,
                document_id=tu_doc.id,
                clause_number=clause.clause_number,
                category=DBRequirementCategory(category.value),
                text=clause.text,
                fragment_id=clause.fragment_id,
                page=clause.page,
            )
            db.add(db_requirement)
            db.flush()
            existing_by_key[key] = db_requirement
            analysis["created"] += 1
        else:
            db_requirement.category = DBRequirementCategory(category.value)
            db_requirement.text = clause.text
            db_requirement.fragment_id = clause.fragment_id
            db_requirement.page = clause.page
            analysis["updated"] += 1
        db_requirements[db_requirement.id] = db_requirement
        tu_clauses[db_requirement.id] = clause
        tu_schemas.append(
            TURequirement(
                id=db_requirement.id,
                clause_number=clause.clause_number,
                category=category,
                text=clause.text,
                fragment_id=clause.fragment_id,
                page=clause.page,
                fragment_text=clause.fragment_text,
            )
        )

    # Stale requirements survive if an engineer confirmed them or an accepted finding points to them.
    for requirement in existing_requirements:
        if requirement.id in db_requirements:
            continue
        has_human_verdict = any(
            verification.requirement_id == requirement.id
            and verification.is_confirmed_by_human
            for verification in existing_verifications
        )
        has_accepted_finding = any(
            finding.review_status == FindingReviewStatus.accepted
            and _finding_matches(finding, requirement)
            for finding in existing_findings
        )
        if payload.force or not (has_human_verdict or has_accepted_finding):
            for verification in existing_verifications:
                if verification.requirement_id == requirement.id:
                    db.delete(verification)
            db.delete(requirement)
            analysis["removed"] += 1
    db.flush()

    znp_schemas: list[ZNPClause] = []
    znp_clauses: dict[str, Clause] = {}
    for idx, clause in enumerate(znp_raw, start=1):
        clause_id = f"znp-{idx}"
        znp_clauses[clause_id] = clause
        znp_schemas.append(
            ZNPClause(
                id=clause_id,
                clause_number=clause.clause_number,
                text=clause.text,
                fragment_id=clause.fragment_id,
                page=clause.page,
                fragment_text=clause.fragment_text,
            )
        )

    response = agent.verify_all(tu_schemas, znp_schemas)
    _attach_provenance(response.checked_requirements, tu_schemas, znp_schemas)
    _apply_quote_provenance(
        response.checked_requirements, tu_clauses, znp_clauses, fragments_by_id,
    )
    for item in response.checked_requirements:
        db_requirement = db_requirements.get(item.requirement.id)
        if db_requirement is not None and item.tu_fragment_id:
            db_requirement.fragment_id = item.tu_fragment_id
            db_requirement.page = item.tu_page

    matched_finding_ids: set[str] = set()
    for item in response.checked_requirements:
        matched_clause_number = (
            item.matched_clause.clause_number if item.matched_clause else None
        )
        matched_clause_text = item.matched_clause.text if item.matched_clause else None

        verification = verification_by_requirement.get(item.requirement.id)
        preserved_human = bool(
            verification is not None
            and verification.is_confirmed_by_human
            and not payload.force
        )

        if verification is None:
            verification = RequirementVerification(
                id=item.id,
                project_id=project_id,
                requirement_id=item.requirement.id,
                status=DBRequirementStatus(item.status.value),
                confidence_score=item.confidence_score,
                matched_znp_clause_number=matched_clause_number,
                matched_znp_text=matched_clause_text,
                matched_znp_fragment_id=item.znp_fragment_id,
                matched_znp_page=item.znp_page,
                tu_quote=item.tu_quote,
                znp_quote=item.znp_quote,
                reasoning=item.reasoning,
                is_confirmed_by_human=False,
            )
            db.add(verification)
            verification_by_requirement[item.requirement.id] = verification
        elif preserved_human:
            verification.matched_znp_clause_number = matched_clause_number
            verification.matched_znp_text = matched_clause_text
            verification.matched_znp_fragment_id = item.znp_fragment_id
            verification.matched_znp_page = item.znp_page
            analysis["preserved_human"] += 1
            item.id = verification.id
            item.confidence_score = verification.confidence_score or 0.0
            item.tu_quote = verification.tu_quote
            item.znp_quote = verification.znp_quote
            item.reasoning = verification.reasoning
            item.is_confirmed_by_human = True
            item.human_override_status = (
                RequirementStatus(verification.human_override_status.value)
                if verification.human_override_status
                else None
            )
            item.human_comment = verification.human_comment
            if item.human_override_status is not None:
                item.status = item.human_override_status
        else:
            verification.status = DBRequirementStatus(item.status.value)
            verification.confidence_score = item.confidence_score
            verification.matched_znp_clause_number = matched_clause_number
            verification.matched_znp_text = matched_clause_text
            verification.matched_znp_fragment_id = item.znp_fragment_id
            verification.matched_znp_page = item.znp_page
            verification.tu_quote = item.tu_quote
            verification.znp_quote = item.znp_quote
            verification.reasoning = item.reasoning
            if payload.force:
                verification.is_confirmed_by_human = False
                verification.human_override_status = None
                verification.human_comment = None
            item.id = verification.id

        db_requirement = db_requirements[item.requirement.id]
        current_finding = next(
            (
                finding
                for finding in existing_findings
                if finding.id not in matched_finding_ids
                and _finding_matches(finding, db_requirement)
            ),
            None,
        )
        if current_finding is not None:
            matched_finding_ids.add(current_finding.id)

        if (
            current_finding is not None
            and current_finding.review_status != FindingReviewStatus.pending
            and not payload.force
        ):
            analysis["preserved_findings"] += 1
            continue

        needs_finding = item.status in (
            RequirementStatus.missing,
            RequirementStatus.needs_review,
        )
        if not needs_finding:
            if current_finding is not None:
                db.delete(current_finding)
            continue

        evidence_list = [
            {
                "document_id": tu_doc.id,
                "requirement_id": item.requirement.id,
                "fragment_id": item.tu_fragment_id,
                "page": item.tu_page if item.tu_page is not None else 1,
                "quote": item.tu_quote or item.requirement.text[:200],
            }
        ]
        if item.matched_clause and item.znp_quote:
            evidence_list.append({
                "document_id": znp_doc.id,
                "requirement_id": item.requirement.id,
                "fragment_id": item.znp_fragment_id,
                "page": item.znp_page if item.znp_page is not None else 1,
                "quote": item.znp_quote,
            })

        finding_type = (
            "missing_requirement"
            if item.status == RequirementStatus.missing
            else "needs_review"
        )
        severity = "error" if item.status == RequirementStatus.missing else "warning"
        suggested_action = (
            "Дополнить ЗнП пропущенным требованием ТУ"
            if item.status == RequirementStatus.missing
            else "Проверить соответствие параметров ТУ и ЗнП инженером"
        )
        if current_finding is None:
            db.add(
                Finding(
                    project_id=project_id,
                    module="requirements",
                    type=finding_type,
                    severity=severity,
                    explanation=f"[{item.requirement.clause_number}] {item.reasoning}",
                    suggested_action=suggested_action,
                    evidence=evidence_list,
                )
            )
        else:
            current_finding.type = finding_type
            current_finding.severity = severity
            current_finding.explanation = (
                f"[{item.requirement.clause_number}] {item.reasoning}"
            )
            current_finding.suggested_action = suggested_action
            current_finding.evidence = evidence_list
            if payload.force:
                current_finding.review_status = FindingReviewStatus.pending

    for finding in existing_findings:
        if finding.id in matched_finding_ids:
            continue
        if payload.force or finding.review_status == FindingReviewStatus.pending:
            db.delete(finding)

    if response.trace_info is None:
        response.trace_info = {}
    response.trace_info["analysis"] = analysis

    db.commit()
    return response


@router.get("/projects/{project_id}", response_model=CheckRequirementsResponse)
def get_project_requirements(project_id: str, db: Session = Depends(get_db)):
    """Return persisted TU/ZnP comparison results for a project."""
    get_project(db, project_id)

    requirements = db.scalars(
        select(DBTURequirement)
        .where(DBTURequirement.project_id == project_id)
        .order_by(DBTURequirement.created_at, DBTURequirement.id)
    ).all()
    verifications = db.scalars(
        select(RequirementVerification)
        .where(RequirementVerification.project_id == project_id)
        .order_by(RequirementVerification.created_at)
    ).all()
    findings = db.scalars(
        select(Finding).where(
            Finding.project_id == project_id,
            Finding.module == "requirements",
        )
    ).all()
    accepted_ids = _accepted_requirement_ids(findings, requirements)

    verifications_by_requirement: dict[str, list[RequirementVerification]] = {}
    for verification in verifications:
        verifications_by_requirement.setdefault(
            verification.requirement_id, [],
        ).append(verification)

    # One card per stable requirement key. Human verdicts win, then an accepted
    # finding, then the freshest verification.
    cards: dict[
        tuple[str, str, str], tuple[DBTURequirement, RequirementVerification]
    ] = {}
    for requirement in requirements:
        own = verifications_by_requirement.get(requirement.id)
        if not own:
            continue
        verification = _best_verification(own, accepted_ids)
        if verification is None:
            continue
        key = _requirement_key(
            requirement.document_id, requirement.clause_number, requirement.text,
        )
        current = cards.get(key)
        if (
            current is None
            or _verification_rank(verification, accepted_ids)
            > _verification_rank(current[1], accepted_ids)
        ):
            cards[key] = (requirement, verification)

    fragment_ids = {
        requirement.fragment_id
        for requirement in requirements
        if requirement.fragment_id
    } | {
        verification.matched_znp_fragment_id
        for verification in verifications
        if verification.matched_znp_fragment_id
    }
    fragment_texts: dict[str, str] = {}
    if fragment_ids:
        fragment_texts = {
            fragment.id: fragment.text
            for fragment in db.scalars(
                select(Fragment).where(Fragment.id.in_(fragment_ids))
            ).all()
        }

    items: list[RequirementCheckItem] = []
    for requirement, verification in cards.values():
        effective_status = RequirementStatus(
            (verification.human_override_status or verification.status).value
        )
        matched = (
            ZNPClause(
                id=f"znp-{verification.id[:8]}",
                clause_number=verification.matched_znp_clause_number or "—",
                text=verification.matched_znp_text or "",
                fragment_id=verification.matched_znp_fragment_id,
                page=verification.matched_znp_page,
                fragment_text=(
                    fragment_texts.get(verification.matched_znp_fragment_id)
                    if verification.matched_znp_fragment_id
                    else None
                ),
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
                    fragment_id=requirement.fragment_id,
                    page=requirement.page,
                    fragment_text=(
                        fragment_texts.get(requirement.fragment_id)
                        if requirement.fragment_id
                        else None
                    ),
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
                tu_fragment_id=requirement.fragment_id,
                tu_page=requirement.page,
                tu_fragment_text=(
                    fragment_texts.get(requirement.fragment_id)
                    if requirement.fragment_id
                    else None
                ),
                znp_fragment_id=verification.matched_znp_fragment_id,
                znp_page=verification.matched_znp_page,
                znp_fragment_text=(
                    fragment_texts.get(verification.matched_znp_fragment_id)
                    if verification.matched_znp_fragment_id
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
