"""Persistence and project-level serialization for TU/ZnP comparison results."""
import threading
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.models import (
    Document, DocumentKind, Finding, Fragment, Project, RequirementAnalysis,
    RequirementStatus as DBRequirementStatus, RequirementVerification,
    ReviewState, ReviewStatus as FindingReviewStatus, TURequirement as DBTURequirement,
)
from app.requirements.extraction import normalize_clause_text
from app.requirements.schemas import (
    CheckRequirementsResponse, ConfirmRequirementRequest, RequirementCategory,
    RequirementCheckItem, RequirementStatus, TURequirement, ZNPClause,
)


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


def finding_matches(finding: Finding, requirement: DBTURequirement) -> bool:
    """Prefer the stable evidence link; keep the old clause prefix for legacy rows."""
    linked_id = _finding_requirement_id(finding)
    if linked_id is not None:
        return linked_id == requirement.id
    return finding.explanation.startswith(f"[{requirement.clause_number}]")


def finding_in_pair(finding: Finding, tu_document_id: str, znp_document_id: str | None) -> bool:
    evidence = [e for e in finding.evidence or [] if isinstance(e, dict)]
    return any(
        e.get("document_id") == tu_document_id
        and e.get("znp_document_id") == znp_document_id
        for e in evidence
        if "znp_document_id" in e
    ) or (
        znp_document_id is not None
        and {tu_document_id, znp_document_id} <= {e.get("document_id") for e in evidence}
    )


def confirm_requirement(project_id: str, payload: ConfirmRequirementRequest, db: Session):
    requirement = db.get(DBTURequirement, payload.requirement_id)
    if requirement is None or requirement.project_id != project_id:
        raise HTTPException(404, "TU requirement not found in project")
    if payload.znp_document_id:
        document = db.get(Document, payload.znp_document_id)
        if document is None or document.project_id != project_id or document.kind != DocumentKind.znp:
            raise HTTPException(422, "ZNP document must belong to this project")
    choices = db.scalars(
        select(RequirementVerification).where(
            RequirementVerification.project_id == project_id,
            RequirementVerification.requirement_id == requirement.id,
        )
    ).all()
    if payload.znp_document_id:
        choices = [v for v in choices if v.znp_document_id == payload.znp_document_id]
    if payload.verification_id:
        verification = next((v for v in choices if v.id == payload.verification_id), None)
        if verification is None:
            raise HTTPException(404, "Verification not found for this requirement")
    else:
        if len(choices) > 1:
            raise HTTPException(409, "Specify verification_id for the document pair")
        verification = choices[0] if choices else None
    if verification is None:
        verification = RequirementVerification(
            project_id=project_id, requirement_id=requirement.id,
            znp_document_id=payload.znp_document_id,
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
        if (finding_matches(finding, requirement)
                and finding_in_pair(finding, requirement.document_id, verification.znp_document_id)):
            finding.review_status = (
                FindingReviewStatus.rejected
                if payload.status == RequirementStatus.fulfilled
                else FindingReviewStatus.accepted
            )

    db.commit()
    db.refresh(verification)
    return verification


def requirement_key(document_id: str, clause_number: str, text: str) -> tuple[str, str, str]:
    return (document_id, clause_number, normalize_clause_text(text))


# Analyses are serialized per project. PostgreSQL also locks the project row,
# which protects deployments with several API workers.
_ANALYSIS_LOCKS: dict[str, threading.Lock] = {}
_ANALYSIS_LOCKS_GUARD = threading.Lock()


def analysis_lock(project_id: str) -> threading.Lock:
    with _ANALYSIS_LOCKS_GUARD:
        return _ANALYSIS_LOCKS.setdefault(project_id, threading.Lock())


def lock_project_for_analysis(db: Session, project_id: str) -> Project:
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


def accepted_requirement_ids(
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
                if finding_matches(finding, requirement)
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


def best_verification(
    verifications: list[RequirementVerification], accepted_ids: set[str],
) -> RequirementVerification | None:
    return max(
        verifications,
        key=lambda verification: _verification_rank(verification, accepted_ids),
        default=None,
    )


def deduplicate_requirements(
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
    accepted_ids = accepted_requirement_ids(findings, requirements)
    by_requirement: dict[str, list[RequirementVerification]] = {}
    for verification in verifications:
        by_requirement.setdefault(verification.requirement_id, []).append(verification)

    groups: dict[tuple[str, str, str], list[DBTURequirement]] = {}
    for requirement in requirements:
        groups.setdefault(
            requirement_key(
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
        by_pair: dict[str | None, list[RequirementVerification]] = {}
        for verification in group_verifications:
            by_pair.setdefault(verification.znp_document_id, []).append(verification)
        keepers = [best_verification(items, accepted_ids) for items in by_pair.values()]

        def survivor_rank(requirement: DBTURequirement):
            best = best_verification(
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
        for keeper in keepers:
            keeper.requirement_id = survivor.id
        for verification in group_verifications:
            if verification not in keepers:
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

        runs = db.scalars(select(RequirementAnalysis).where(
            RequirementAnalysis.tu_document_id == survivor.document_id)).all()
        for run in runs:
            run.requirement_ids = list(dict.fromkeys(
                survivor.id if item in group_ids else item for item in run.requirement_ids))

        for requirement in group:
            if requirement is not survivor:
                db.delete(requirement)
                removed += 1
    return removed


def load_project_requirements(db: Session, project_id: str, tu_document_id: str | None = None,
                              znp_document_id: str | None = None):
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
    if tu_document_id:
        requirements = [r for r in requirements if r.document_id == tu_document_id]
    if znp_document_id:
        verifications = [v for v in verifications if v.znp_document_id == znp_document_id]
    accepted_ids = accepted_requirement_ids(findings, requirements)

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
        by_pair: dict[str | None, list[RequirementVerification]] = {}
        for verification in own:
            by_pair.setdefault(verification.znp_document_id, []).append(verification)
        for pair_id, pair_verifications in by_pair.items():
            verification = best_verification(pair_verifications, accepted_ids)
            key = (*requirement_key(requirement.document_id, requirement.clause_number, requirement.text), pair_id)
            current = cards.get(key)
            if current is None or _verification_rank(verification, accepted_ids) > _verification_rank(current[1], accepted_ids):
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
                automatic_status=RequirementStatus(verification.status.value),
                confidence_score=verification.confidence_score or 0.0,
                matched_clause=matched,
                tu_quote=verification.tu_quote,
                znp_quote=verification.znp_quote,
                reasoning=verification.reasoning,
                validation_details=verification.validation_details,
                method=verification.method,
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
