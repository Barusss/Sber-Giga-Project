"""Compare extracted normative references with the local registry and Rosstandart.

The local registry is the primary source for availability: a previously
confirmed active/obsolete status is reused without a network call. Missing or
unknown records are looked up officially; ``refresh=True`` bypasses the cache
but still falls back to the archived record when the official site is down.
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
    db: Session, document: Document, registry: RosstandartClient | None = None,
    refresh: bool = False,
) -> VerificationReport:
    """Use the local registry first; query the official one only when needed."""
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
        
        # The local registry is the primary source when it already contains a
        # usable status. This keeps verification available during Rosstandart
        # outages; missing/unknown entries still go to the official registry.
        local_normative = local_cache.get(code)
        local_status = local_normative.status if local_normative else None
        local_usable = local_status in {"active", "obsolete"}

        official: RegistryResult | None = None
        if local_usable and not refresh:
            final_status = local_status
            source = "local"
            registry_status = local_normative.registry_status
            final_replacement = local_normative.replacement
            final_title = local_normative.title
            final_source_url = local_normative.source_url
            checked_at = local_normative.checked_at
            reason = "local_registry_hit"
        else:
            hint = local_normative.source_url if local_normative else None
            official = registry.lookup(code, source_url=hint)
            confirmed = (
                official.status in {"active", "obsolete"}
                and bool(official.source_url)
                and official.checked_at is not None
            )
            if confirmed:
                final_status = official.status
                source = "rosstandart"
                registry_status = official.registry_status
                final_replacement = official.replacement
                final_title = official.title or (
                    local_normative.title if local_normative else None
                )
                final_source_url = official.source_url
                checked_at = official.checked_at
                reason = official.reason

                # Persist the confirmed card so subsequent checks can work
                # offline without inventing a fresh timestamp.
                if local_normative is None:
                    local_normative = Normative(
                        code=code,
                        title=final_title or code,
                        status=official.status,
                        registry_status=official.registry_status,
                        replacement=official.replacement,
                        source_url=official.source_url,
                        checked_at=official.checked_at,
                    )
                    db.add(local_normative)
                    local_cache[code] = local_normative
                else:
                    local_normative.title = final_title or local_normative.title
                    local_normative.status = official.status
                    local_normative.registry_status = official.registry_status
                    local_normative.replacement = official.replacement
                    local_normative.source_url = official.source_url
                    local_normative.checked_at = official.checked_at
            elif local_usable:
                # A forced refresh failed: keep the archived record instead of
                # dropping to unknown, but label why it was used.
                final_status = local_status
                source = "local"
                registry_status = local_normative.registry_status
                final_replacement = local_normative.replacement
                final_title = local_normative.title
                final_source_url = local_normative.source_url
                checked_at = local_normative.checked_at
                reason = "refresh_failed_local_fallback"
            else:
                final_status = "unknown"
                source = "unknown"
                registry_status = official.registry_status
                final_replacement = None
                final_title = (
                    local_normative.title if local_normative else official.title
                )
                final_source_url = None
                checked_at = None
                reason = official.reason or "official_verification_unavailable"

        checked.append(StandardCheck(
            code=code,
            reference_kind=role,
            status=final_status,
            registry_status=registry_status,
            replacement=final_replacement,
            title=final_title,
            source_url=final_source_url,
            checked_at=checked_at,
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
        snapshot.registry_status = registry_status
        snapshot.title = final_title
        snapshot.replacement = final_replacement
        snapshot.source_url = final_source_url
        snapshot.checked_at = checked_at
        snapshot.attempted_at = now()
        snapshot.reason = reason
        snapshot.source = source

        # Retain an auditable informational finding for obsolete bibliography
        # references, but do not present it as a normative requirement.
        if final_status != "obsolete" or not evidence:
            continue

        already = next(
            (item for item in existing_findings if finding_key(item, document.id, code)),
            None
        )
        if already is not None:
            found += 1
            continue

        replacement_text = f" Замена по реестру: {final_replacement}." if final_replacement else ""
        provenance = (
            "официальный статус (Росстандарт)"
            if source == "rosstandart"
            else "статус по локальному реестру"
        )
        checked_text = (
            f" Дата подтверждения: {checked_at:%d.%m.%Y}."
            if checked_at else " Дата подтверждения не указана."
        )
        explanation = (
            f"Упоминание {code}: {provenance} «{registry_status or final_status}»."
            f"{replacement_text}{checked_text} Источник: {final_source_url or 'не указан'}"
        )
        
        if role == "informative":
            guidance = (
                "Ссылка носит справочный характер. Проверьте необходимость "
                "её обновления; замена не обязательна автоматически."
            )
        elif final_replacement:
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
            severity="info" if role == "informative" else "warning",
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