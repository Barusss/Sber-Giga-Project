"""Compare extracted normative references with live Rosstandart registry cards.

Only a verified Rosstandart response can establish active/obsolete status.
Local registry records remain lookup hints, never fresh official evidence.
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
    source: str | None  # "local" | "rosstandart" | "unknown"


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
    """Verify official statuses without treating saved statuses as freshly checked."""
    facts = db.scalars(
        select(Fact).where(
            Fact.document_id == document.id,
            Fact.fact_type == "normative_reference",
            Fact.review_status != ReviewStatus.rejected,
        ).options(selectinload(Fact.evidence).selectinload(FactEvidence.fragment))
    ).all()
    
    # Группируем факты по нормализованному коду
    grouped: dict[str, list[Fact]] = {}
    for fact in facts:
        if not isinstance(fact.value, dict):
            continue
        code = fact.value.get("code")
        if isinstance(code, str) and code.strip():
            grouped.setdefault(normalize_code(code), []).append(fact)
            
    if len(grouped) > MAX_STANDARDS:
        raise HTTPException(413, f"Maximum {MAX_STANDARDS} standards per verification request")

    # Кэш локального реестра
    local_cache = {
        normalize_code(row.code): row for row in db.scalars(select(Normative)).all()
    }
    
    # Кэш предыдущих проверок
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
                    "normative_code": code,
                })
        pages = sorted({entry["page"] for entry in evidence})
        
        # Проверяем локальный реестр
        local_normative = local_cache.get(code)
        local_status = local_normative.status if local_normative else None
        local_replacement = local_normative.replacement if local_normative else None
        local_title = local_normative.title if local_normative else None
        local_source_url = local_normative.source_url if local_normative else None
        
        # Обращаемся к Росстандарту
        hint = local_source_url
        status: RegistryResult = registry.lookup(code, source_url=hint)
        
        # Local data can hint which official card to query, but never counts as
        # current official verification, even if previously confirmed.
        confirmed = (
            status.status in {"active", "obsolete"}
            and bool(status.source_url)
            and status.checked_at is not None
        )
        if confirmed:
            final_status = status.status
            source = "rosstandart"
            final_replacement = status.replacement
            final_title = status.title or local_title
            final_source_url = status.source_url
            reason = status.reason
        else:
            final_status = "unknown"
            source = "local" if local_status in {"active", "obsolete"} else "unknown"
            final_replacement = None
            final_title = local_title or status.title
            final_source_url = None
            reason = status.reason or "official_verification_unavailable"

        checked.append(StandardCheck(
            code=code,
            reference_kind=role,
            status=final_status,
            registry_status=status.registry_status,
            replacement=final_replacement,
            title=final_title,
            source_url=final_source_url,
            checked_at=status.checked_at,
            pages=pages,
            reason=reason,
            source=source,
        ))
        
        # Обновляем snapshot проверки
        snapshot = snapshots.get(code)
        if snapshot is None:
            snapshot = NormativeVerification(document_id=document.id, code=code)
            db.add(snapshot)
            snapshots[code] = snapshot
        snapshot.reference_kind = role
        snapshot.status = final_status
        snapshot.registry_status = status.registry_status if confirmed else None
        snapshot.title = final_title
        snapshot.replacement = final_replacement
        snapshot.source_url = final_source_url
        snapshot.checked_at = status.checked_at if confirmed else None
        snapshot.attempted_at = now()
        snapshot.reason = reason

        # Обновляем локальный реестр, если Росстандарт подтвердил
        if confirmed:
            if local_normative is None:
                local_normative = Normative(
                    code=code,
                    title=final_title or code,
                    status=status.status,
                    replacement=status.replacement,
                    source_url=status.source_url,
                )
                db.add(local_normative)
                local_cache[code] = local_normative
            else:
                local_normative.title = final_title or local_normative.title
                local_normative.status = status.status
                local_normative.replacement = status.replacement
                local_normative.source_url = status.source_url

        # Создаём Finding только для устаревших нормативных ссылок
        if final_status != "obsolete" or role != "normative" or not evidence:
            continue

        already = next(
            (item for item in existing_findings if finding_key(item, document.id, code)),
            None
        )
        if already is not None:
            found += 1
            continue

        replacement_text = f" Замена по реестру: {final_replacement}." if final_replacement else ""
        source_text = "Росстандарт" if source == "rosstandart" else "локальный реестр"
        explanation = (
            f"Упоминание {code}: официальный статус ({source_text}) «{status.registry_status or final_status}»."
            f"{replacement_text} Источник: {final_source_url or 'не указан'}"
        )
        
        if final_replacement:
            guidance = (
                f"Проверьте применимость {final_replacement} вместо {code} "
                "и необходимость изменения ссылки в документе. Решение принимает эксперт."
            )
        else:
            guidance = (
                f"Для {code} замена не указана. "
                "Определите действующий нормативный документ и оцените необходимость изменения ссылки. "
                "Решение принимает эксперт."
            )
            
        finding = Finding(
            project_id=document.project_id,
            module="normatives",
            type=FINDING_TYPE,
            severity="warning",
            explanation=explanation,
            suggested_action=guidance,
            evidence=evidence,
        )
        db.add(finding)
        existing_findings.append(finding)
        created += 1

    db.commit()
    return VerificationReport(
        document_id=document.id,
        total=len(checked),
        active=sum(item.status == "active" for item in checked),
        obsolete=sum(item.status == "obsolete" for item in checked),
        unknown=sum(item.status == "unknown" for item in checked),
        findings_created=created,
        findings_already_existing=found,
        checked=checked,
    )