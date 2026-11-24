"""Source completeness ledger for conservative ZnP extraction.

Never infer that a parser fragment is safe to ignore just because it was
not extracted into an atom. Only unambiguously administrative/empty fragments
receive an excluded role; remaining gaps need manual classification.

The ledger contains provenance hashes and IDs, never private source text.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import re
from typing import Sequence

from app.core.models import Fragment
from app.requirements.atomization import DocumentAtom
from app.requirements.extraction import is_noise, is_toc_line, is_table_header, is_table_description, is_table_caption


def _declares_incomplete(text: str) -> bool:
    return bool(re.search(r'не\s+полный\s+документ|показаны\s+только\s+выбранные\s+исходные\s+строки', text, re.I))


def _skip_reason(fragment: Fragment) -> str | None:
    text = (fragment.text or "").strip()
    if not text:
        return "empty"
    if fragment.block_type == "page_furniture":
        return "page_furniture"
    if fragment.block_type == "toc" or is_toc_line(text):
        return "toc"
    if text.casefold().strip(" .:") in ("оглавление", "содержание"):
        return "toc_heading"
    if text.casefold().strip(' .:') == 'задание на проектирование':
        return 'document_title'
    # A short table cell such as "10", "6" or "А" can be an electrical
    # parameter. Never classify it as disposable noise just because it is
    # short or numeric. The same applies to standalone numeric values.
    if fragment.block_type == "table_row" or text.isdecimal():
        return None
    if is_noise(text):
        return "noise"
    return None


_NORMATIVE_CATALOG_RE = re.compile(
    r"\b(?:перечень|список)\s+нормативн\w*\s+"
    r"(?:документ\w*|акт\w*)\b",
    re.IGNORECASE,
)
_NUMBERED_REFERENCE_RE = re.compile(
    r"^\s*\d{1,4}[.)]\s+(?:ГОСТ|СП\s|СНиП|СН\s|СТО\s|ПУЭ|"
    r"РД\s|Правила\s|Приказ\s)",
    re.IGNORECASE,
)


def _review_hint(fragment: Fragment) -> str | None:
    """A triage hint is NOT a source role and never clears a safety gate."""
    text = (fragment.text or "").strip()
    section = fragment.section_path or ""
    if is_table_header(text) or is_table_description(text) or is_table_caption(text):
        return 'structural_table_context_unverified'
    if (_NORMATIVE_CATALOG_RE.search(section)
            and _NUMBERED_REFERENCE_RE.match(text)):
        return "normative_reference_candidate"
    return None


def source_coverage_ledger(
    fragments: Sequence[Fragment],
    atoms: Sequence[DocumentAtom],
) -> dict:
    """Classify each fragment as source, context, excluded, or unclassified.

    This audits the full *parser* output, not the semantic search candidates.
    No generated verdict or LLM response influences classification.
    """
    source_ids = {s.fragment_id for atom in atoms for s in atom.sources}
    context_ids = {s.fragment_id for atom in atoms for s in atom.context_sources}
    incompleteness_ids = {f.id for f in fragments if _declares_incomplete(f.text or '')}
    ranges = {}
    for atom in atoms:
        for source in (*atom.sources, *atom.context_sources):
            ranges.setdefault(source.fragment_id, []).append(source)
    by_role = Counter()
    unclassified_by_block_type = Counter()
    excluded_by_reason = Counter()
    review_hints = Counter()
    records = []
    seen = set()
    for fragment in fragments:
        if fragment.id in seen:
            raise ValueError(f"Duplicate source fragment ID: {fragment.id}")
        seen.add(fragment.id)
        reason = None
        covered = [False] * len(fragment.text or "")
        for source in ranges.get(fragment.id, []):
            if (source.start_offset < 0 or source.end_offset > len(covered)
                    or source.start_offset >= source.end_offset
                    or fragment.text[source.start_offset:source.end_offset] != source.text):
                raise ValueError("Invalid source span")
            covered[source.start_offset:source.end_offset] = [True] * (source.end_offset - source.start_offset)
        partial = bool(ranges.get(fragment.id)) and any(
            not checked and not char.isspace()
            for checked, char in zip(covered, fragment.text)
        )
        if partial:
            role = "unclassified"
            reason = "partial_source_coverage"
            unclassified_by_block_type[fragment.block_type or "unknown"] += 1
        elif fragment.id in source_ids:
            role = "atom_source"
        elif fragment.id in context_ids:
            role = "context_only"
            if fragment.id in incompleteness_ids:
                reason = 'document_incompleteness_notice'
        elif fragment.id in incompleteness_ids:
            # A notice can coexist with technical material. Only a standalone
            # notice may be classified without a verified source/context span.
            if re.fullmatch(r'\s*(?:внимание\s*[:!—-]?\s*)?не\s+полный\s+документ[.!\s]*',
                            fragment.text or '', re.I):
                role = 'context_only'
                reason = 'document_incompleteness_notice'
            else:
                role = 'unclassified'
                reason = 'document_incompleteness_notice_unverified'
                unclassified_by_block_type[fragment.block_type or "unknown"] += 1
        else:
            reason = _skip_reason(fragment)
            role = "excluded" if reason else "unclassified"
            if role == "unclassified":
                unclassified_by_block_type[fragment.block_type or "unknown"] += 1
            else:
                excluded_by_reason[reason] += 1
        hint = _review_hint(fragment) if role == "unclassified" else None
        if hint is not None:
            review_hints[hint] += 1
        by_role[role] += 1
        records.append({
            "fragment_id": fragment.id,
            "page": fragment.page,
            "position": fragment.position,
            "block_type": fragment.block_type,
            "container_id": getattr(fragment, "container_id", None),
            "section_hash": sha256(
                (fragment.section_path or "").encode("utf-8")
            ).hexdigest(),
            "text_sha256": sha256(
                (fragment.text or "").encode("utf-8")
            ).hexdigest(),
            "role": role,
            "reason": reason,
            "review_hint": hint,
        })
    missing = (source_ids | context_ids) - seen
    if missing:
        raise ValueError(
            f"Atom provenance refers to {len(missing)} absent parser fragments"
        )
    if sum(by_role.values()) != len(fragments):
        raise AssertionError("Source coverage must account for all parser fragments")
    return {
        "schema": "znp-source-coverage-v1",
        "total_fragments": len(fragments),
        "atoms": len(atoms),
        "by_role": dict(by_role),
        "unclassified_by_block_type": dict(unclassified_by_block_type),
        "excluded_by_reason": dict(excluded_by_reason),
        "review_hint_counts": dict(review_hints),
        "unclassified_fragment_ids": [
            item["fragment_id"] for item in records
            if item["role"] == "unclassified"
        ],
        "unclassified_count": by_role["unclassified"],
        "complete": by_role["unclassified"] == 0 and not incompleteness_ids,
        "fragment_coverage_complete": by_role["unclassified"] == 0,
        "document_completeness": 'declared_incomplete' if incompleteness_ids else 'not_declared',
        "incompleteness_fragment_ids": sorted(incompleteness_ids),
        "records": records,
    }


def source_coverage_summary(ledger: dict) -> dict:
    """Compact report-safe summary, not a list of private fragment contents."""
    return {
        **{key: ledger[key] for key in (
            "schema", "total_fragments", "atoms", "by_role",
            "unclassified_by_block_type", "excluded_by_reason",
            "review_hint_counts", "unclassified_fragment_ids",
            "unclassified_count", "complete",
        )},
        **{key: ledger[key] for key in ('fragment_coverage_complete', 'document_completeness',
                                        'incompleteness_fragment_ids') if key in ledger},
    }


def apply_source_coverage_guard(response, ledger: dict | None) -> None:
    """Keep a finished AI pass visible, but NEVER auto-fulfill an incomplete ZnP.

    Unclassified fragments may contain crucial annex restrictions. They must
    first be explained or recovered, regardless of how optimistic the LLM is.
    This modifies a fresh automated response, not historical human decisions.
    """
    from app.requirements.schemas import RequirementStatus

    if response.trace_info is None:
        response.trace_info = {}
    unknown = ledger is None
    response.trace_info["source_coverage"] = (
        {"schema": "znp-source-coverage-v1", "status": "unknown", "complete": False,
         "unclassified_count": None, "reason": "Source coverage was not calculated"}
        if unknown else source_coverage_summary(ledger)
    )
    if not unknown and ledger["complete"] is True:
        return
    response.trace_info["degraded"] = True
    downgraded = 0
    for item in response.checked_requirements:
        if item.status != RequirementStatus.fulfilled:
            continue
        item.status = RequirementStatus.needs_review
        item.automatic_status = RequirementStatus.needs_review
        item.confidence_score = 0.0
        suffix = "Полнота исходного ЗнП неизвестна: проверка покрытия источников отсутствует." if unknown else (
            "Полнота исходного ЗнП не подтверждена: "
            f"{ledger['unclassified_count']} фрагментов не классифицированы. "
            "Автоматическое соответствие отложено до проверки источников."
        )
        if not unknown and ledger.get('document_completeness') == 'declared_incomplete':
            suffix = 'В исходном документе явно указана неполнота/выборка. Автоматическое соответствие требует проверки полного исходного документа.'
        item.reasoning = ((item.reasoning or "").rstrip() + " " + suffix).strip()
        details = dict(item.validation_details or {})
        details["source_coverage_incomplete"] = True
        item.validation_details = details
        downgraded += 1
    response.trace_info["source_coverage"]["downgraded_fulfilled"] = downgraded
    response.stats = {
        status.value: sum(
            item.status == status for item in response.checked_requirements
        ) for status in RequirementStatus
    }
    response.stats["total"] = len(response.checked_requirements)
