from dataclasses import asdict
from typing import Callable
"""Participant #3 TU/ZnP module: storage, human review and AI-assisted matching.

Manual input and expert confirmations are always available. The automated
comparison endpoint uses an evidence-checked agent and falls back to a
deterministic local pipeline when the LLM is not configured. Results are
persisted as requirements/verifications/findings, and a human verdict remains
the final decision.
"""
import logging
import re
import threading
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.db import get_db
from app.documents.parser import TABLE_REVIEW, requires_pdf_layout_review
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
    RequirementAnalysis,
    ReviewState,
    ReviewStatus as FindingReviewStatus,
    TURequirement as DBTURequirement,
)
from app.ai import diagnostics
from app.requirements.agent import RequirementsAgent
from app.requirements.atomization import atomize_fragments, atomize_document
from app.requirements.source_coverage import (
    source_coverage_ledger, source_coverage_summary,
    apply_source_coverage_guard,
)
from app.requirements.semantic_atomization import semantic_atomize, atomization_workload
from app.requirements.budget_planning import plan_full_run
from app.requirements.preview_selection import select_technical_preview
from app.requirements.tools import _list_items
from app.requirements.llm_tracking import TrackedRequirementsLLM
from app.requirements.coverage import build_obligations
from app.core.config import settings
from app.requirements.znp_first import ZnPFirstAgent
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
logger = logging.getLogger(__name__)


def _safe_deepseek_error_code(error: object) -> str:
    """Return only a fixed diagnostic label, never provider or document content."""
    if not isinstance(error, str):
        return "unknown"
    status = re.search(r"\bHTTP\s+([1-5]\d\d)\b", error)
    if status is not None:
        return "HTTP_" + status.group(1)
    for name in (
        "ConnectError", "ConnectTimeout", "ReadTimeout", "ReadError",
        "WriteTimeout", "WriteError", "PoolTimeout", "RemoteProtocolError",
    ):
        if re.search(r"\b" + name + r"\b", error):
            return name
    descriptions = (
        ("truncated", "response_truncated"),
        ("invalid json", "invalid_json"),
        ("malformed", "malformed_response"),
        ("empty content", "empty_response"),
        ("did not finish", "incomplete_response"),
        ("not configured", "not_configured"),
    )
    lower = error.casefold()
    for phrase, label in descriptions:
        if phrase in lower:
            return label
    return "unspecified"


def _report_failed_ai_comparison(
    trace: dict, ledger: TrackedRequirementsLLM | None, *,
    mode: str,
) -> str:
    """Log only error category and numeric progress; no prompts or document data."""
    failure_kind = trace.get("model_failure_kind")
    error_text = next(
        (
            step.get("reason")
            for run in reversed(trace.get("agent_runs") or [])
            for step in reversed(run.get("steps") or [])
            if step.get("tool") == "llm_fallback"
        ),
        None,
    )
    code = _safe_deepseek_error_code(trace.get("model_failure_reason") or error_text)
    if code == "unspecified":
        if failure_kind == "DeepSeekConnectionError":
            code = "connection_setup_failed"
        elif failure_kind == "DeepSeekResponseError":
            code = "response_error"
        elif failure_kind == "DeepSeekAPIError":
            code = "request_or_http_error"
    usage = ledger.trace() if ledger is not None else (trace.get("llm_usage") or {})
    successes = usage.get("responses_by_phase") or {}
    errors = usage.get("errors_by_phase") or {}
    attempts = usage.get("attempted_by_phase") or {}
    failed_phases = [name for name, count in errors.items() if count]
    phase = ",".join(sorted(failed_phases)) if failed_phases else "unknown"
    checked_atoms = sum(
        bool(run.get("model_checked"))
        for run in trace.get("agent_runs") or []
    )
    logger.error(
        "DeepSeek comparison failure mode=%s type=%s code=%s phase=%s "
        "calls=%d responses=%d errors=%d checked_znp=%d "
        "validated_links=%d",
        mode,
        failure_kind or "unknown",
        code,
        phase,
        sum(attempts.values()),
        sum(successes.values()),
        sum(errors.values()),
        checked_atoms,
        int(trace.get("validated_links") or 0),
    )
    return (
        f"type={failure_kind or 'unknown'}, code={code}, phase={phase}, "
        f"responses={sum(successes.values())}"
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


def _finding_matches(finding: Finding, requirement: DBTURequirement) -> bool:
    """Prefer the stable evidence link; keep the old clause prefix for legacy rows."""
    linked_id = _finding_requirement_id(finding)
    if linked_id is not None:
        return linked_id == requirement.id
    return finding.explanation.startswith(f"[{requirement.clause_number}]")


def _finding_in_pair(finding: Finding, tu_document_id: str, znp_document_id: str | None) -> bool:
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
    with _analysis_lock(project_id):
        _lock_project_for_analysis(db, project_id)
        return _confirm_requirement(project_id, payload, db)


def _confirm_requirement(project_id: str, payload: ConfirmRequirementRequest, db: Session):
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
        if (_finding_matches(finding, requirement)
                and _finding_in_pair(finding, requirement.document_id, verification.znp_document_id)):
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
        by_pair: dict[str | None, list[RequirementVerification]] = {}
        for verification in group_verifications:
            by_pair.setdefault(verification.znp_document_id, []).append(verification)
        keepers = [_best_verification(items, accepted_ids) for items in by_pair.values()]

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
    """Find the fragment containing the quote, or the first for an absent quote."""
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
        return None
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
    znp_document_id: str | None = None,
) -> None:
    for item in items:
        tu_clause = tu_clauses.get(item.requirement.id)
        tu_fragment = _fragment_for_quote(item.tu_quote, tu_clause, fragments_by_id)
        if tu_fragment is not None:
            item.tu_fragment_id = tu_fragment.id
            item.tu_page = tu_fragment.page
            item.tu_fragment_text = tu_fragment.text
            item.tu_quote = _shorten_to_fragment(item.tu_quote, tu_fragment)
        elif tu_clause is not None and item.tu_quote:
            item.tu_quote = None
            item.tu_fragment_id = None
            item.tu_page = None
            item.tu_fragment_text = None
            if item.status == RequirementStatus.fulfilled:
                item.status = RequirementStatus.needs_review
                item.reasoning = "Требует проверки: цитата ТУ не привязана к одному исходному фрагменту."

        if item.matched_clause is None:
            continue
        znp_clause = znp_clauses.get(item.matched_clause.id)
        znp_fragment = _fragment_for_quote(item.znp_quote, znp_clause, fragments_by_id)
        if znp_fragment is not None:
            item.znp_fragment_id = znp_fragment.id
            item.znp_page = znp_fragment.page
            item.znp_fragment_text = znp_fragment.text
            item.znp_quote = _shorten_to_fragment(item.znp_quote, znp_fragment)
        elif item.znp_quote and not (item.validation_details or {}).get("items"):
            item.znp_quote = None
            item.znp_fragment_id = None
            item.znp_page = None
            item.znp_fragment_text = None
            if item.status == RequirementStatus.fulfilled:
                item.status = RequirementStatus.needs_review
                item.reasoning = "Требует проверки: цитата ЗнП не привязана к одному исходному фрагменту."

    for item in items:
        details = item.validation_details or {}
        list_items = details.get("items")
        if not isinstance(list_items, list):
            continue
        first_evidence = None
        for list_item in list_items:
            validated = []
            proposed_evidence = list_item.get("evidence", [])
            for evidence in proposed_evidence:
                clause = znp_clauses.get(evidence.get("clause_id"))
                if clause is None:
                    continue
                quote = evidence.get("quote") or ""
                fragment = next((fragments_by_id[fragment_id] for fragment_id in clause.fragment_ids
                    if fragment_id in fragments_by_id
                    and fragments_by_id[fragment_id].document_id == znp_document_id
                    and _normalize_ws(quote) in _normalize_ws(fragments_by_id[fragment_id].text)), None)
                if fragment is None or not quote.strip():
                    continue
                validated_entry = {"document_id": fragment.document_id, "fragment_id": fragment.id,
                    "page": fragment.page, "clause_number": clause.clause_number, "quote": quote}
                validated.append(validated_entry)
                if first_evidence is None:
                    first_evidence = (validated_entry, clause, fragment)
            list_item["evidence"] = validated
            if not validated or len(validated) != len(proposed_evidence):
                list_item["status"] = "needs_review"
        if any(entry.get("status") != "fulfilled" for entry in list_items):
            if item.status == RequirementStatus.fulfilled:
                item.status = RequirementStatus.needs_review
            count = sum(entry.get("status") == "fulfilled" for entry in list_items)
            prefix = "Требует проверки."
            if item.status == RequirementStatus.missing:
                prefix = "Соответствие не установлено."
            explanation = f"{prefix} Подтверждено элементов: {count} из {len(list_items)}."
            if item.reasoning and "цитата ТУ не привязана" in item.reasoning:
                explanation = item.reasoning + " " + explanation
            item.reasoning = explanation
        if first_evidence:
            entry, clause, fragment = first_evidence
            item.matched_clause = ZNPClause(id="source", clause_number=clause.clause_number, text=clause.text,
                fragment_id=fragment.id, page=fragment.page, fragment_text=fragment.text)
            item.znp_quote = entry["quote"]
            item.znp_fragment_id = fragment.id
            item.znp_page = fragment.page
            item.znp_fragment_text = fragment.text
        else:
            item.matched_clause = None
            item.znp_quote = None
            item.znp_fragment_id = None
            item.znp_page = None
            item.znp_fragment_text = None


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
        if any(TABLE_REVIEW in item.warnings for item in (*payload.tu_requirements, *payload.znp_clauses)):
            agent = ZnPFirstAgent(ai_client=agent.ai, embedder=agent.embedder)
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




def _analyze_test_subset(
    tu_doc: Document,
    znp_doc: Document,
    tu_raw: list[Clause],
    db: Session,
    agent: ZnPFirstAgent,
    progress_callback: Callable[[str, int, int, str | None], None] | None,
) -> CheckRequirementsResponse:
    """Read-only 10 x 10 preview. Never write requirements, findings or verdicts.

    Restrict source inputs BEFORE DeepSeek atomization. Keep every child atom
    of those ten source clauses so splitting cannot silently drop evidence.
    """
    selected_tu = select_technical_preview(tu_raw, limit=10)
    diagnostics.record(
        "comparison.preview_selected", tu_count=len(selected_tu),
        targeted_disabled=True,
    )
    znp_fragments = db.scalars(
        select(Fragment).where(Fragment.document_id == znp_doc.id)
        .order_by(Fragment.page, Fragment.position)
    ).all()
    original_atoms = atomize_fragments(znp_doc.id, znp_fragments)
    source_atoms = select_technical_preview(original_atoms, limit=10)
    if not selected_tu or not source_atoms:
        raise HTTPException(
            422, "Test mode needs eligible technical TU and ZnP clauses; "
                 "legal preambles and standards lists are excluded",
        )

    # One candidate batch per ZnP atom. Coverage budget reflects the ACTUAL
    # number of selected obligations, rather than 15 fixed requests for any
    # input. Nontechnical legal catalogs are excluded before paid work.
    agent.batch_size = 10
    agent.max_batches = 1
    # Preview tests the primary ZnP -> TU route and TU coverage only.
    # Reverse targeted matching is a separate, costly full-run fallback.
    agent.targeted_enabled = False
    expected_obligations = sum(
        max(1, len(_list_items(clause.text))) for clause in selected_tu
    )
    coverage_budget = min(30, max(16, expected_obligations * 2))
    ledger = TrackedRequirementsLLM(
        agent.ai,
        max_calls=min(55, settings.requirements_llm_max_calls),
        atomization_limit=min(7, settings.requirements_llm_atomization_calls),
        matching_limit=20,
        coverage_limit=coverage_budget,
        targeted_limit=0,
        contradictions_limit=3,
    )
    agent.ai = ledger
    atomization = semantic_atomize(source_atoms, ledger)
    chosen_atoms = atomization.atoms

    tu_schemas: list[TURequirement] = []
    tu_by_id: dict[str, Clause] = {}
    for index, clause in enumerate(selected_tu, start=1):
        rid = str(uuid5(
            NAMESPACE_URL,
            f"preview:{tu_doc.id}:{index}:{clause.clause_number}:{clause.text}",
        ))
        tu_by_id[rid] = clause
        tu_schemas.append(TURequirement(
            id=rid,
            clause_number=clause.clause_number,
            category=classify_requirement_category(clause.text),
            text=clause.text,
            fragment_id=clause.fragment_id,
            page=clause.page,
            fragment_text=clause.fragment_text,
            section_path=clause.section_path,
            sources=[{
                "fragment_id": fid,
                "page": fragment.page,
                "text": fragment.text.strip(),
            } for fid in clause.fragment_ids
              if (fragment := db.get(Fragment, fid)) is not None],
        ))

    znp_schemas: list[ZNPClause] = []
    znp_by_id: dict[str, Clause] = {}
    for atom in chosen_atoms:
        first = atom.sources[0]
        znp_by_id[atom.id] = Clause(
            clause_number=atom.clause_number,
            text=atom.text,
            page=first.page,
            fragment_id=first.fragment_id,
            fragment_ids=[source.fragment_id for source in atom.sources],
            section_path=atom.section_path,
            fragment_text=first.text,
        )
        znp_schemas.append(ZNPClause(
            id=atom.id,
            clause_number=atom.clause_number,
            text=atom.text,
            fragment_id=first.fragment_id,
            page=first.page,
            fragment_text=first.text,
            section_path=atom.section_path,
            sources=[asdict(source) for source in atom.sources],
            context_sources=[asdict(source) for source in atom.context_sources],
            warnings=list(atom.warnings),
        ))

    if progress_callback:
        progress_callback("preparing", 0, len(tu_schemas), None)
    result = agent.verify_all(
        tu_schemas, znp_schemas, progress_callback=progress_callback,
    )
    primary_checked = sum(
        bool(trace.get("model_checked"))
        for trace in (result.trace_info or {}).get("agent_runs", [])
    )
    if primary_checked != len(znp_schemas):
        # No snapshot is saved and no global analysis rows are modified.
        # A failed mandatory search must not masquerade as a completed test.
        detail = _report_failed_ai_comparison(
            result.trace_info or {}, ledger, mode="test",
        )
        raise HTTPException(
            503,
            f"Test AI matching incomplete: {primary_checked} of "
            f"{len(znp_schemas)} ZnP atoms checked ({detail}). "
            "The previous comparison result was preserved.",
        )
    _attach_provenance(result.checked_requirements, tu_schemas, znp_schemas)
    fragments = {
        fragment.id: fragment
        for fragment in db.scalars(select(Fragment).where(
            Fragment.id.in_({
                fid for clause in selected_tu for fid in clause.fragment_ids
            } | {
                source.fragment_id for atom in chosen_atoms for source in atom.sources
            })
        )).all()
    }
    _apply_quote_provenance(
        result.checked_requirements, tu_by_id, znp_by_id, fragments, znp_doc.id,
    )
    # Provenance validation may downgrade a status; recompute the snapshot stats.
    result.stats = {
        status.value: sum(row.status == status for row in result.checked_requirements)
        for status in RequirementStatus
    }
    result.stats["total"] = len(result.checked_requirements)
    if result.trace_info is None:
        result.trace_info = {}
    result.trace_info["test_mode"] = {
        "enabled": True,
        "tu_limit": 10,
        "znp_limit": 10,
        "tu_selected": len(tu_schemas),
        "tu_obligations_estimated": expected_obligations,
        "coverage_call_budget": coverage_budget,
        "targeted_enabled": False,
        "technical_selection": True,
        "znp_source_clauses_selected": len(source_atoms),
        "znp_atoms_selected": len(znp_schemas),
        "znp_atoms_after_split": len(atomization.atoms),
        "truncated_after_atomization": False,
        "isolated": True,
    }
    result.trace_info["atomization"] = atomization.trace()
    result.trace_info["llm_usage"] = ledger.trace()
    if atomization.degraded or ledger.trace()["limit_reached"]:
        result.trace_info["degraded"] = True
    return result


@diagnostics.trace_comparison
def _analyze_project_requirements(
    payload: AnalyzeProjectRequirementsRequest,
    db: Session,
    agent: RequirementsAgent,
    progress_callback: Callable[[str, int, int, str | None], None] | None = None,
    on_published: Callable[[CheckRequirementsResponse, Session], None] | None = None,
) -> CheckRequirementsResponse:
    project_id = payload.project_id
    if progress_callback:
        progress_callback("reading", 0, 0, None)

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
    if tu_doc.kind != DocumentKind.tu:
        raise HTTPException(422, "Selected TU document must have kind=tu")
    if znp_doc.kind != DocumentKind.znp:
        raise HTTPException(422, "Selected ZNP document must have kind=znp")

    pdf_review = any(requires_pdf_layout_review(doc) for doc in (tu_doc, znp_doc))
    if pdf_review and payload.test_mode:
        raise HTTPException(422, f"{TABLE_REVIEW}: используйте полную проверку источников.")
    if pdf_review and not isinstance(agent, ZnPFirstAgent):
        agent = ZnPFirstAgent(ai_client=agent.ai, embedder=agent.embedder)

    tu_raw = [
        clause
        for clause in _extract_clauses_from_doc(db, tu_doc)
        if not is_service_clause(clause.text)
    ]
    tu_atoms = {}
    if requires_pdf_layout_review(tu_doc):
        tu_fragments = db.scalars(select(Fragment).where(
            Fragment.document_id == tu_doc.id
        ).order_by(Fragment.page, Fragment.position)).all()
        for atom in atomize_document(tu_doc, tu_fragments):
            tu_atoms[(atom.clause_number, atom.text)] = atom
        if tu_atoms:
            tu_raw = [Clause(
                clause_number=a.clause_number, text=a.text, page=a.sources[0].page,
                fragment_id=a.sources[0].fragment_id,
                fragment_ids=list(dict.fromkeys(s.fragment_id for s in a.sources)),
                section_path=a.section_path, fragment_text=a.sources[0].text,
            ) for a in tu_atoms.values() if not is_service_clause(a.text)]
    znp_raw = _extract_clauses_from_doc(db, znp_doc)
    diagnostics.record(
        "comparison.clauses_extracted",
        tu_clauses=len(tu_raw), znp_clauses=len(znp_raw),
        tu_chars=sum(len(x.text) for x in tu_raw),
        znp_chars=sum(len(x.text) for x in znp_raw),
        tu_longest=max((len(x.text) for x in tu_raw), default=0),
        znp_longest=max((len(x.text) for x in znp_raw), default=0),
    )
    if not tu_raw or not znp_raw:
        raise HTTPException(422, "No useful TU requirements or ZNP clauses extracted; check OCR and document structure")
    if progress_callback:
        progress_callback("preparing", 0, len(tu_raw), None)

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

    if payload.test_mode:
        if not isinstance(agent, ZnPFirstAgent):
            raise HTTPException(422, "Test mode requires ZnP-first agent")
        return _analyze_test_subset(
            tu_doc, znp_doc, tu_raw, db, agent, progress_callback,
        )

    # The project-workspace comparison uses conservative ZnP atoms, not the
    # legacy numbered-clause splitting. Each atom retains exact source spans.
    conservative_atoms = []
    atomization_trace = None
    source_coverage = None
    llm_ledger = None
    znp_fragments = db.scalars(
        select(Fragment)
        .where(Fragment.document_id == znp_doc.id)
        .order_by(Fragment.page, Fragment.position)
    ).all()
    fragments_by_id.update({fragment.id: fragment for fragment in znp_fragments})
    if znp_fragments:
        original_atoms = atomize_document(znp_doc, znp_fragments)
        source_coverage = source_coverage_ledger(znp_fragments, original_atoms)
        diagnostics.record(
            "comparison.source_coverage",
            **{key: value for key, value in source_coverage_summary(
                source_coverage,
            ).items() if key != "unclassified_fragment_ids"},
        )
        if (source_coverage["unclassified_count"] > 0
                and not payload.allow_incomplete_sources):
            # Stop BEFORE semantic atomization, retrieval and the first
            # paid call. UI and API users must knowingly authorize an
            # incomplete (never auto-fulfilled) report.
            count = source_coverage["unclassified_count"]
            diagnostics.record(
                "comparison.source_coverage_blocked",
                unclassified_count=count, no_paid_calls=True,
            )
            raise HTTPException(
                422,
                f"ZnP source coverage incomplete: {count} unclassified "
                "fragments. No DeepSeek calls were made. "
                "Review source coverage or explicitly set "
                "allow_incomplete_sources=true to continue with "
                "automatic fulfilled verdicts disabled.",
            )
    if isinstance(agent, ZnPFirstAgent):
        llm_ledger = TrackedRequirementsLLM(
            agent.ai,
            max_calls=settings.requirements_llm_max_calls,
            atomization_limit=settings.requirements_llm_atomization_calls,
            targeted_limit=settings.requirements_llm_targeted_calls,
        )
        agent.ai = llm_ledger
        if znp_fragments:
            diagnostics.record(
                "comparison.source_atoms", count=len(original_atoms),
                max_chars=max((len(a.text) for a in original_atoms), default=0),
            )
            # Evaluate affordability before the FIRST paid LLM call.
            workload = atomization_workload(original_atoms)
            plan = plan_full_run(
                tu_clauses=tu_raw,
                znp_source_atoms=len(original_atoms),
                atomization_eligible=workload["eligible"],
                max_calls=llm_ledger.max_calls,
                atomization_limit=llm_ledger.phase_limits["atomization"],
                targeted_limit=llm_ledger.phase_limits["targeted"],
                matching_batches=agent.max_batches,
                targeted_enabled=agent.targeted_enabled,
            )
            diagnostics.record("comparison.budget_preflight", **plan.as_dict())
            if plan.blockers:
                diagnostics.record(
                    "comparison.budget_preflight_refused",
                    blockers=plan.blockers, no_paid_calls=True,
                )
                raise HTTPException(status_code=422, detail=plan.refusal_message())
            semantic_result = semantic_atomize(original_atoms, agent.ai)
            conservative_atoms = semantic_result.atoms
            atomization_trace = semantic_result.trace()
            diagnostics.record("comparison.atomization_complete", **atomization_trace)
            znp_raw = [
                Clause(
                    clause_number=atom.clause_number,
                    text=atom.text,
                    page=atom.sources[0].page,
                    fragment_id=atom.sources[0].fragment_id,
                    fragment_ids=[source.fragment_id for source in atom.sources],
                    section_path=atom.section_path,
                    fragment_text=atom.sources[0].text,
                )
                for atom in conservative_atoms
            ]
            if not znp_raw:
                raise HTTPException(
                    422, "No traceable ZnP statements extracted; check document structure"
                )
        else:
            # Legacy chunk fallback: no semantic atomization, but still check
            # the full-run global cap before any model call.
            plan = plan_full_run(
                tu_clauses=tu_raw,
                znp_source_atoms=len(znp_raw),
                atomization_eligible=0,
                max_calls=llm_ledger.max_calls,
                atomization_limit=llm_ledger.phase_limits["atomization"],
                targeted_limit=llm_ledger.phase_limits["targeted"],
                matching_batches=agent.max_batches,
                targeted_enabled=agent.targeted_enabled,
            )
            diagnostics.record("comparison.budget_preflight", **plan.as_dict())
            if plan.blockers:
                diagnostics.record(
                    "comparison.budget_preflight_refused",
                    blockers=plan.blockers, no_paid_calls=True,
                )
                raise HTTPException(status_code=422, detail=plan.refusal_message())

    existing_requirements = db.scalars(
        select(DBTURequirement).where(DBTURequirement.project_id == project_id,
                                      DBTURequirement.document_id == tu_doc.id)
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
            select(DBTURequirement).where(DBTURequirement.project_id == project_id,
                                      DBTURequirement.document_id == tu_doc.id)
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
    existing_findings = [f for f in existing_findings if _finding_in_pair(f, tu_doc.id, znp_doc.id)]
    accepted_ids = _accepted_requirement_ids(existing_findings, existing_requirements)
    verifications_by_requirement: dict[str, list[RequirementVerification]] = {}
    for verification in existing_verifications:
        if verification.znp_document_id != znp_doc.id:
            continue
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
        tu_atom = tu_atoms.get((clause.clause_number, clause.text))
        category = classify_requirement_category(clause.text)
        key = _requirement_key(tu_doc.id, clause.clause_number, clause.text)
        db_requirement = existing_by_key.get(key)
        if db_requirement is None:
            # Stable IDs are required to recreate identical LLM prompts
            # after a crash before this transaction commits.
            stable_id = str(uuid5(NAMESPACE_URL, repr(key)))
            db_requirement = DBTURequirement(
                id=stable_id,
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
                section_path=clause.section_path,
                sources=[asdict(s) for s in tu_atom.sources] if tu_atom else [
                    {"fragment_id": fid, "page": fragments_by_id[fid].page,
                     "text": fragments_by_id[fid].text.strip()}
                    for fid in clause.fragment_ids if fid in fragments_by_id
                ],
                warnings=list(tu_atom.warnings) if tu_atom else ([TABLE_REVIEW] if requires_pdf_layout_review(tu_doc) else []),
            )
        )

    # Stale requirements survive if an engineer confirmed them or an accepted finding points to them.
    for requirement in existing_requirements:
        if requirement.id in db_requirements:
            continue
        if any(v.requirement_id == requirement.id and v.znp_document_id != znp_doc.id
               for v in existing_verifications):
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
        atom = conservative_atoms[idx - 1] if conservative_atoms else None
        clause_id = atom.id if atom else f"znp-{idx}"
        znp_clauses[clause_id] = clause
        znp_schemas.append(
            ZNPClause(
                id=clause_id,
                clause_number=clause.clause_number,
                text=clause.text,
                fragment_id=clause.fragment_id,
                page=clause.page,
                fragment_text=clause.fragment_text,
                section_path=clause.section_path,
                sources=[asdict(source) for source in atom.sources] if atom else [],
                context_sources=(
                    [asdict(source) for source in atom.context_sources] if atom else []
                ),
                warnings=list(atom.warnings) if atom else [],
            )
        )

    if llm_ledger is not None and isinstance(agent, ZnPFirstAgent):
        llm_ledger.configure_full_run_limits(
            atom_count=len(znp_schemas),
            obligation_count=sum(len(build_obligations(item)) for item in tu_schemas),
            requirement_count=len(tu_schemas),
            matching_batches=agent.max_batches,
        )

    diagnostics.record(
        "comparison.verification_start", tu_count=len(tu_schemas),
        znp_count=len(znp_schemas),
        budget=llm_ledger.trace() if llm_ledger is not None else {},
    )
    response = (
        agent.verify_all(tu_schemas, znp_schemas, progress_callback=progress_callback)
        if progress_callback else agent.verify_all(tu_schemas, znp_schemas)
    )
    diagnostics.record(
        "comparison.verification_complete",
        status_counts=response.stats,
        degraded=bool((response.trace_info or {}).get("degraded")),
        provider_failure_type=(response.trace_info or {}).get("model_failure_kind"),
        llm_usage=llm_ledger.trace() if llm_ledger is not None else {},
    )
    if response.trace_info is None:
        response.trace_info = {}
    if source_coverage is not None:
        apply_source_coverage_guard(response, source_coverage)
    if atomization_trace is not None:
        response.trace_info["atomization"] = atomization_trace
        if atomization_trace["degraded"]:
            response.trace_info["degraded"] = True
    if llm_ledger is not None:
        response.trace_info["llm_usage"] = llm_ledger.trace()
        if llm_ledger.trace()["limit_reached"]:
            response.trace_info["degraded"] = True
    coverage_unreviewed = (
        (response.trace_info.get("comparison_stages") or {})
        .get("coverage", {}).get("unreviewed", 0)
    )
    if (
        not payload.test_mode
        and isinstance(agent, ZnPFirstAgent)
        and (
            coverage_unreviewed
            or response.trace_info.get("model_failure_kind") in {
                "DeepSeekConnectionError", "DeepSeekAPIError", "DeepSeekResponseError"
            }
        )
    ):
        # Fail before any new result is committed, but preserve diagnostic
        # counters even when the surrounding DB transaction rolls back.
        detail = _report_failed_ai_comparison(
            response.trace_info, llm_ledger, mode="full",
        )
        if coverage_unreviewed and not response.trace_info.get("model_failure_kind"):
            detail = f"coverage_incomplete:{coverage_unreviewed}"
        diagnostics.record(
            "comparison.persistence_blocked",
            failure_kind=response.trace_info.get("model_failure_kind"),
            coverage_unreviewed=coverage_unreviewed,
            llm_usage=llm_ledger.trace() if llm_ledger is not None else {},
        )
        raise HTTPException(
            503,
            f"DeepSeek comparison interrupted ({detail}). "
            "No new result was saved; the previous result remains current.",
        )
    if progress_callback:
        progress_callback("persisting", len(tu_schemas), len(tu_schemas), None)
    _attach_provenance(response.checked_requirements, tu_schemas, znp_schemas)
    _apply_quote_provenance(
        response.checked_requirements, tu_clauses, znp_clauses, fragments_by_id, znp_doc.id,
    )
    for item in response.checked_requirements:
        db_requirement = db_requirements.get(item.requirement.id)
        item.automatic_status = item.status
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
                znp_document_id=znp_doc.id,
                status=DBRequirementStatus(item.status.value),
                method=item.method,
                confidence_score=item.confidence_score,
                matched_znp_clause_number=matched_clause_number,
                matched_znp_text=matched_clause_text,
                matched_znp_fragment_id=item.znp_fragment_id,
                matched_znp_page=item.znp_page,
                tu_quote=item.tu_quote,
                znp_quote=item.znp_quote,
                reasoning=item.reasoning,
                validation_details=item.validation_details,
                is_confirmed_by_human=False,
            )
            db.add(verification)
            verification_by_requirement[item.requirement.id] = verification
        elif preserved_human:
            # Re-analysis must not silently replace the evidence used in a
            # confirmed engineer decision. Keep the original matched passage,
            # quote and source location together with the manual verdict.
            analysis["preserved_human"] += 1
            item.id = verification.id
            item.matched_clause = (
                ZNPClause(
                    id=f"znp-preserved-{verification.id}",
                    clause_number=verification.matched_znp_clause_number or "—",
                    text=verification.matched_znp_text,
                    fragment_id=verification.matched_znp_fragment_id,
                    page=verification.matched_znp_page,
                    fragment_text=(
                        fragments_by_id[verification.matched_znp_fragment_id].text
                        if verification.matched_znp_fragment_id in fragments_by_id
                        else None
                    ),
                )
                if verification.matched_znp_text else None
            )
            item.znp_fragment_id = verification.matched_znp_fragment_id
            item.znp_page = verification.matched_znp_page
            item.znp_fragment_text = (
                fragments_by_id[verification.matched_znp_fragment_id].text
                if verification.matched_znp_fragment_id in fragments_by_id
                else None
            )
            item.confidence_score = verification.confidence_score or 0.0
            item.tu_quote = verification.tu_quote
            item.znp_quote = verification.znp_quote
            item.reasoning = verification.reasoning
            item.automatic_status = RequirementStatus(verification.status.value)
            item.validation_details = verification.validation_details
            item.method = verification.method
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
            verification.method = item.method
            verification.confidence_score = item.confidence_score
            verification.matched_znp_clause_number = matched_clause_number
            verification.matched_znp_text = matched_clause_text
            verification.matched_znp_fragment_id = item.znp_fragment_id
            verification.matched_znp_page = item.znp_page
            verification.tu_quote = item.tu_quote
            verification.znp_quote = item.znp_quote
            verification.reasoning = item.reasoning
            verification.validation_details = item.validation_details
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
                "znp_document_id": znp_doc.id,
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
        for list_item in (item.validation_details or {}).get("items", []):
            for evidence in list_item.get("evidence", []):
                entry = {"document_id": evidence["document_id"], "requirement_id": item.requirement.id,
                    "fragment_id": evidence["fragment_id"], "page": evidence["page"], "quote": evidence["quote"]}
                if entry not in evidence_list:
                    evidence_list.append(entry)

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
    response.stats = {
        status.value: sum(item.status == status for item in response.checked_requirements)
        for status in RequirementStatus
    }
    response.stats["total"] = len(response.checked_requirements)
    completed = db.get(RequirementAnalysis, (tu_doc.id, znp_doc.id))
    if completed is None:
        completed = RequirementAnalysis(tu_document_id=tu_doc.id, znp_document_id=znp_doc.id)
        db.add(completed)
    completed.requirement_ids = [item.requirement.id for item in response.checked_requirements]
    completed.completed_at = datetime.now(timezone.utc)

    # The official snapshot must commit atomically with verifications and
    # findings; otherwise a crash between two commits loses the run history.
    if on_published is not None and not payload.test_mode:
        on_published(response, db)
    db.commit()
    return response


@router.get("/projects/{project_id}", response_model=CheckRequirementsResponse)
def get_project_requirements(project_id: str, tu_document_id: str | None = None,
                             znp_document_id: str | None = None, db: Session = Depends(get_db)):
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
        by_pair: dict[str | None, list[RequirementVerification]] = {}
        for verification in own:
            by_pair.setdefault(verification.znp_document_id, []).append(verification)
        for pair_id, pair_verifications in by_pair.items():
            verification = _best_verification(pair_verifications, accepted_ids)
            key = (*_requirement_key(requirement.document_id, requirement.clause_number, requirement.text), pair_id)
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
