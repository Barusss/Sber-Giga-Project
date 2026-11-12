"""Conservative TU obligation register and source-grounded evidence coverage.

An obligation retains its parent TU clause, including conditional language.
Splitting is restricted to explicit, independently enumerated items. A source
quote is always rechecked against the original TU text.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any

from app.requirements.schemas import TURequirement
from app.requirements.tools import _list_items, _extract_search_parameters


@dataclass(frozen=True)
class TUObligation:
    id: str
    requirement_id: str
    text: str
    source_quote: str
    context: str
    scope: str
    conditions: tuple[dict[str, Any], ...]
    uncertain_scope: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _id(req_id: str, index: int, quote: str) -> str:
    return sha256(f"tu-obligation-v1:{req_id}:{index}:{quote}".encode()).hexdigest()[:24]


def _quote_in_text(text: str, quote: str) -> bool:
    return " ".join(quote.split()).casefold() in " ".join(text.split()).casefold()


def _condition_index(text: str) -> tuple[dict[str, Any], ...]:
    """A diagnostic index, not independent grounds for declaring compliance."""
    return tuple({
        "comparison": item["comparison"],
        "value": item["value"],
        "unit": item["unit_display"],
        "context": item["context"],
    } for item in _extract_search_parameters(text))


def build_obligations(requirement: TURequirement) -> list[TUObligation]:
    """Keep clauses intact unless the author explicitly enumerated obligations."""
    source = requirement.text
    items = _list_items(source)
    valid_items = [item for item in items if _quote_in_text(source, item)]
    if len(valid_items) < 2 or len(valid_items) != len(items):
        valid_items = [source]

    # The lead-in can contain an object, exception or condition applying to
    # the entire list. Never discard it when splitting explicit list items.
    scope = source
    first = valid_items[0]
    if len(valid_items) > 1:
        position = source.casefold().find(first.casefold())
        scope = source[:position].strip() if position > 0 else source
    uncertain = bool(requirement.warnings)
    obligations = []
    for index, quote in enumerate(valid_items, start=1):
        context = source if len(valid_items) > 1 else quote
        obligations.append(TUObligation(
            id=_id(requirement.id, index, quote),
            requirement_id=requirement.id,
            text=quote,
            source_quote=quote,
            context=context,
            scope=scope,
            conditions=_condition_index(quote),
            uncertain_scope=uncertain,
        ))
    return obligations


COVERAGE_PROMPT = """Ты проверяешь ПОКРЫТИЕ одного обязательного условия ТУ доказательствами ЗнП.
Обязательство извлечено непосредственно из ТУ. Доказательства уже проверены
на существование цитат в исходных документах. Это не означает, что их смысл верен.
Проверь, что все существенные характеристики, тот же объект, условия,
исключения, альтернативы, запреты и направления сравнения действительно
подтверждены. Не смешивай оборудование разных объектов.
Если данных недостаточно, covered=false. Если поддержка состоит из нескольких
отрывков, укажи все использованные link_id. Старайся не полагаться на
похожую лексику вместо фактического выполнения требования.
Возвращай только JSON:
{"covered":false,"evidence_ids":[],"object_consistent":false,"reasoning":"..."}
"""


def assess_coverage(
    requirement: TURequirement,
    obligation: TUObligation,
    supports: list[dict[str, Any]],
    ai: Any,
    *,
    max_evidence: int = 35,
) -> dict[str, Any]:
    """Model checks semantics; Python validates selected link IDs and provenance.

    Deliberately never converts a model-unavailable/incomplete check into a
    positive verdict. The source-grounded evidence remains visible for review.
    """
    if not supports:
        return {
            "obligation_id": obligation.id, "text": obligation.text,
            "status": "uncovered", "evidence_ids": [],
            "reasoning": "Нет подтверждающих доказательств",
            "checked": True,
        }
    payload = {
        "tu_requirement": {"id": requirement.id, "text": requirement.text},
        "obligation": obligation.as_dict(),
        "evidence": [{
            "link_id": entry["link_id"],
            "clause_number": entry["clause_number"],
            "tu_quote": entry["tu_quote"],
            "znp_quote": entry["quote"],
            "znp_text": entry["atom_text"],
            "warnings": entry.get("warnings", []),
        } for entry in supports[:max_evidence]],
    }
    try:
        response = ai.complete_json(
            system_prompt=COVERAGE_PROMPT,
            user_prompt=json.dumps(payload, ensure_ascii=False),
        )
    except Exception as exc:
        return {
            "obligation_id": obligation.id, "text": obligation.text,
            "status": "needs_review", "evidence_ids": [],
            "reasoning": "Проверка покрытия не завершилась: " + str(exc)[:150],
            "checked": False,
        }
    allowed = {entry["link_id"]: entry for entry in supports[:max_evidence]}
    selected = response.get("evidence_ids") if isinstance(response, dict) else None
    if not isinstance(selected, list) or not all(
        isinstance(key, str) and key in allowed for key in selected
    ):
        return {
            "obligation_id": obligation.id, "text": obligation.text,
            "status": "needs_review", "evidence_ids": [],
            "reasoning": "Модель не вернула проверяемые идентификаторы доказательств",
            "checked": False,
        }
    chosen = list(dict.fromkeys(selected))
    safe = bool(chosen) and all(
        not allowed[key].get("warnings")
        and allowed[key].get("scope_consistent", True)
        for key in chosen
    )
    # A link was initially checked against the WHOLE TU clause. For an
    # enumerated requirement it can therefore report mismatches simply because
    # the other list members live in separate ZnP clauses. Check the selected
    # evidence against THIS obligation before validating its coverage.
    selected_text = "\n".join(allowed[key]["atom_text"] for key in chosen)
    from app.requirements.tools import tool_verify_engineering_params
    obligation_params = tool_verify_engineering_params(obligation.text, selected_text)
    safe = safe and obligation_params["params_match"]
    complete = (
        response.get("covered") is True
        and response.get("object_consistent") is True
        and safe and not obligation.uncertain_scope
    )
    if len(supports) > max_evidence and re.search(
        r"\b(?:все[хм]?|кажд\w*|люб\w*)\b", obligation.context, re.IGNORECASE
    ):
        # Universal requirements need exhaustive object coverage, not a short
        # representative set. Ordinary existence claims need only sufficient
        # evidence plus the independent contradiction scan.
        complete = False
    return {
        "obligation_id": obligation.id, "text": obligation.text,
        "status": "fulfilled" if complete else "needs_review",
        "evidence_ids": chosen, "checked": True,
        "reasoning": str(response.get("reasoning") or "")[:500],
        "candidates_truncated": len(supports) > max_evidence,
    }


def all_obligations_covered(items: list[dict[str, Any]]) -> bool:
    return bool(items) and all(item["status"] == "fulfilled" for item in items)


def scope_overlap(tu_text: str, znp_text: str) -> bool:
    """Conservative subject gate for independent conflict candidates.

    Hard identifiers such as KL-1 should agree. Without explicit IDs, require
    meaningful non-numeric words in common; do not infer object identity.
    """
    from app.requirements.tools import _extract_identifiers, _content_tokens
    object_pattern = re.compile(r"^[а-яa-z]{1,8}-?\d{1,4}(?:[./-]\d+)?$", re.IGNORECASE)
    # Numeric parameters such as '150 кВт' are NOT object identifiers:
    # treating them as IDs would hide the very 150/200 kW conflicts we seek.
    tu_ids = {x for x in _extract_identifiers(tu_text) if object_pattern.fullmatch(x)}
    znp_ids = {x for x in _extract_identifiers(znp_text) if object_pattern.fullmatch(x)}
    if tu_ids and not (tu_ids & znp_ids):
        return False
    shared = _content_tokens(tu_text) & _content_tokens(znp_text)
    return len(shared) >= 1


def conflict_candidates(requirement: TURequirement, atoms: list[Any]) -> list[dict[str, Any]]:
    """Scan ALL ZnP atoms independently of the positive retrieval results.

    A numeric discrepancy is an allegation, not a certified contradiction.
    An actual verdict requires a subsequent source-grounded model review.
    """
    from app.requirements.tools import tool_verify_engineering_params
    candidates = []
    # Non-numeric deontic oppositions require a separate semantic audit too.
    # This is candidate discovery only: DeepSeek must confirm the opposition.
    deontic = re.compile(
        r"\b(?:запрещ\w*|допуска\w*|не\s+допуска\w*|"
        r"не\s+предусмотр\w*|исключ\w*|отсутств\w*)\b",
        re.IGNORECASE,
    )
    tu_deontic = bool(deontic.search(requirement.text))
    for atom in atoms:
        if not scope_overlap(requirement.text, atom.text):
            continue
        diff = tool_verify_engineering_params(requirement.text, atom.text)
        if ("mismatch" in diff["mismatch_types"]
                or (tu_deontic and deontic.search(atom.text))
                or (deontic.search(atom.text) and not tu_deontic)):
            candidates.append({
                "atom_id": atom.id,
                "text": atom.text,
                "clause_number": atom.clause_number,
                "mismatches": diff["mismatches"],
                "sources": getattr(atom, "sources", ()) or (),
                "fragment_id": getattr(atom, "fragment_id", None),
                "page": getattr(atom, "page", None),
            })
    return candidates


CONFLICT_PROMPT = """Независимая проверка возможных ПРОТИВОРЕЧИЙ между требованием ТУ
и положениями ЗнП. Список сформирован отдельно от поиска подтверждений.
Разные объекты, разные условия, исключения и альтернативы нельзя смешивать.
Выдели только реальные противоречия. При сомнении верни ID в uncertain_ids.
Цитаты должны быть точными подстроками соответствующих документов.
Верни JSON:
{"conflicts":[{"atom_id":"...","tu_quote":"...","znp_quote":"...","reasoning":"..."}],
 "uncertain_ids":[]}
"""


def assess_conflicts(
    requirement: TURequirement, candidates: list[dict[str, Any]], ai: Any,
    *,
    batch_size: int = 8,
) -> tuple[list[dict[str, Any]], list[str], int]:
    """Validate a complete independent contradiction candidate scan in batches."""
    confirmed = []
    uncertain = []
    calls = 0
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i:i + batch_size]
        payload = {
            "requirement": {"id": requirement.id, "text": requirement.text},
            "candidates": batch,
        }
        calls += 1
        try:
            response = ai.complete_json(
                system_prompt=CONFLICT_PROMPT,
                user_prompt=json.dumps(payload, ensure_ascii=False),
            )
        except Exception:
            uncertain.extend(x["atom_id"] for x in batch)
            continue
        if not isinstance(response, dict) or not isinstance(response.get("conflicts"), list) or not isinstance(response.get("uncertain_ids"), list):
            uncertain.extend(x["atom_id"] for x in batch)
            continue
        by_id = {x["atom_id"]: x for x in batch}
        seen = set()
        for suggestion in response["conflicts"]:
            if not isinstance(suggestion, dict):
                continue
            key = suggestion.get("atom_id")
            candidate = by_id.get(key)
            if candidate is None or key in seen:
                continue
            seen.add(key)
            tu_quote, znp_quote = suggestion.get("tu_quote"), suggestion.get("znp_quote")
            if not isinstance(tu_quote, str) or not isinstance(znp_quote, str) or not _quote_in_text(requirement.text, tu_quote) or not _quote_in_text(candidate["text"], znp_quote) or not tu_quote.strip() or not znp_quote.strip():
                uncertain.append(key)
                continue
            tu_sources = requirement.sources
            if tu_sources and not any(_quote_in_text(
                source.get("text", "") if isinstance(source, dict) else source.text,
                tu_quote,
            ) for source in tu_sources):
                uncertain.append(key)
                continue
            if not tu_sources and requirement.fragment_text and not _quote_in_text(requirement.fragment_text, tu_quote):
                uncertain.append(key)
                continue
            # A quote spanning stitched OCR fragments is not a valid citation.
            sources = candidate["sources"]
            resolved = None
            for source in sources:
                raw = source.get("text", "") if isinstance(source, dict) else source.text
                if _quote_in_text(raw, znp_quote):
                    resolved = source
                    break
            if sources and resolved is None:
                uncertain.append(key)
                continue
            if resolved is None:
                raw = candidate["text"]
                if not _quote_in_text(raw, znp_quote):
                    uncertain.append(key)
                    continue
                fragment_id, page = candidate["fragment_id"], candidate["page"]
            elif isinstance(resolved, dict):
                fragment_id, page = resolved.get("fragment_id"), resolved.get("page")
            else:
                fragment_id, page = resolved.fragment_id, resolved.page
            confirmed.append({
                "atom_id": key, "clause_number": candidate["clause_number"],
                "fragment_id": fragment_id, "page": page,
                "tu_quote": tu_quote, "quote": znp_quote,
                "relation": "conflicts",
                "reasoning": str(suggestion.get("reasoning") or "")[:500],
                "param_check": {"mismatches": candidate["mismatches"]},
            })
        for key in response["uncertain_ids"]:
            if key in by_id and key not in seen:
                uncertain.append(key)
                seen.add(key)
        # Do not equate omission from an LLM response with confirmed safety:
        # explicit empty conflicts + empty uncertain means candidates cleared.
    return confirmed, sorted(set(uncertain)), calls
