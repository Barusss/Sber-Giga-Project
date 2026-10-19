"""Compare extracted normative references with live Rosstandart registry cards.

Persist confirmed status to the local registry and create reviewable findings.
Network problems never produce an 'active' or 'obsolete' claim.
"""
from datetime import datetime

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.models import (
    Document, Fact, FactEvidence, Finding, Normative, NormativeVerification, ReviewStatus, now,
)
from app.normatives.extraction import normalize_code
from app.normatives.registry import RegistryResult, RosstandartClient

MAX_STANDARDS = 50
FINDING_TYPE = "obsolete_normative"


class StandardCheck(BaseModel):
    code: str
    reference_kind: str
    status: str
    registry_status: str | None
    replacement: str | None
    title: str | None
    source_url: str | None
    checked_at: datetime | None
    pages: list[int]
    reason: str | None


class VerificationReport(BaseModel):
    document_id: str
    total: int
    active: int
    obsolete: int
    unknown: int
    findings_created: int
    findings_already_existing: int
    checked: list[StandardCheck]


def finding_key(finding: Finding, document_id: str, code: str) -> bool:
    if finding.module != "normatives" or finding.type != FINDING_TYPE:
        return False
    return any(
        item.get("document_id") == document_id and item.get("normative_code") == code
        for item in finding.evidence if isinstance(item, dict)
    )


def check_document_standards(
    db: Session, document: Document, registry: RosstandartClient | None = None
) -> VerificationReport:
    facts = db.scalars(
        select(Fact).where(
            Fact.document_id == document.id,
            Fact.fact_type == "normative_reference",
            Fact.review_status != ReviewStatus.rejected,
        ).options(selectinload(Fact.evidence).selectinload(FactEvidence.fragment))
    ).all()
    # Consolidate identical codes even if the document contains a manual fact.
    grouped: dict[str, list[Fact]] = {}
    for fact in facts:
        if not isinstance(fact.value, dict):
            continue
        code = fact.value.get("code")
        if isinstance(code, str) and code.strip():
            grouped.setdefault(normalize_code(code), []).append(fact)
    if len(grouped) > MAX_STANDARDS:
        raise HTTPException(413, f"Maximum {MAX_STANDARDS} standards per verification request")

    cache = {
        normalize_code(row.code): row for row in db.scalars(select(Normative)).all()
    }
    snapshots = {
        row.code: row for row in db.scalars(
            select(NormativeVerification).where(NormativeVerification.document_id == document.id)
        ).all()
    }
    existing_findings = db.scalars(
        select(Finding).where(
            Finding.project_id == document.project_id,
            Finding.module == "normatives",
            Finding.type == FINDING_TYPE,
        )
    ).all()
    registry = registry or RosstandartClient()
    checked = []
    created = found = 0

    for code in sorted(grouped):
        appearances = grouped[code]
        role = "informative"
        if any(f.value.get("reference_kind") != "informative" for f in appearances):
            role = "normative"
        evidence = []
        seen = set()
        for fact in appearances:
            for item in fact.evidence:
                key = (item.fragment_id, item.quote)
                if key in seen:
                    continue
                seen.add(key)
                evidence.append({
                    "document_id": document.id,
                    "fragment_id": item.fragment_id,
                    "page": item.fragment.page,
                    "quote": item.quote,
                    "normative_code": code,  # internal key; not exposed in FindingOut
                })
        pages = sorted({entry["page"] for entry in evidence})
        previous = cache.get(code)
        hint = previous.source_url if previous else None
        status: RegistryResult = registry.lookup(code, source_url=hint)

        checked.append(StandardCheck(
            code=code, reference_kind=role, status=status.status,
            registry_status=status.registry_status, replacement=status.replacement,
            title=status.title, source_url=status.source_url,
            checked_at=status.checked_at, pages=pages, reason=status.reason,
        ))
        # Always persist the last attempt, including unknown results. Never display
        # a previous successful lookup as if the most recent attempt succeeded.
        confirmed = status.status in {"active", "obsolete"} and bool(status.source_url)
        snapshot = snapshots.get(code)
        if snapshot is None:
            snapshot = NormativeVerification(document_id=document.id, code=code)
            db.add(snapshot)
            snapshots[code] = snapshot
        snapshot.reference_kind = role
        snapshot.status = status.status if confirmed else "unknown"
        snapshot.registry_status = status.registry_status if confirmed else None
        snapshot.title = status.title if confirmed else None
        snapshot.replacement = status.replacement if confirmed else None
        snapshot.source_url = status.source_url if confirmed else None
        snapshot.checked_at = status.checked_at if confirmed else None
        snapshot.attempted_at = now()
        snapshot.reason = status.reason if not confirmed else None

        if not confirmed:
            continue

        # Local registry contains only the most recent verified snapshot.
        if previous is None:
            previous = Normative(
                code=code, title=status.title or code, status=status.status,
                replacement=status.replacement, source_url=status.source_url,
            )
            db.add(previous)
            cache[code] = previous
        else:
            previous.title = status.title or previous.title
            previous.status = status.status
            previous.replacement = status.replacement
            previous.source_url = status.source_url

        if status.status != "obsolete" or not evidence:
            continue

        already = next(
            (item for item in existing_findings if finding_key(item, document.id, code)),
            None
        )
        if already is not None:
            found += 1
            continue

        replacement = f" Замена по реестру: {status.replacement}." if status.replacement else ""
        explanation = (
            f"Упоминание {code}: официальный статус Росстандарта «{status.registry_status}»."
            f"{replacement} Источник: {status.source_url}"
        )
        guidance = (
            "Проверьте применимость заменяющего стандарта и необходимость исправления "
            "ссылки в документе. Это замечание требует проверки специалистом."
        )
        finding = Finding(
            project_id=document.project_id, module="normatives",
            type=FINDING_TYPE, severity=("warning" if role == "normative" else "info"),
            explanation=explanation, suggested_action=guidance,
            evidence=evidence,
        )
        db.add(finding)
        existing_findings.append(finding)
        created += 1

    db.commit()
    return VerificationReport(
        document_id=document.id, total=len(checked),
        active=sum(item.status == "active" for item in checked),
        obsolete=sum(item.status == "obsolete" for item in checked),
        unknown=sum(item.status == "unknown" for item in checked),
        findings_created=created, findings_already_existing=found, checked=checked,
    )
