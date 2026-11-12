"""P0: TU obligation coverage, targeted retry, independent contradiction audit."""
import json

from app.ai.embeddings import EmbeddingService
from app.requirements.coverage import build_obligations, scope_overlap
from app.requirements.schemas import TURequirement, ZNPClause, RequirementCategory
from app.requirements.znp_first import ZnPFirstAgent


def req(text):
    return TURequirement(
        id="tu-1", clause_number="1.1",
        category=RequirementCategory.other, text=text,
        fragment_id="tu-fragment", fragment_text=text,
    )


def znp(index, text):
    return ZNPClause(
        id=f"z-{index}", clause_number=str(index), text=text,
        fragment_id=f"z-frag-{index}", fragment_text=text,
        sources=[{"fragment_id": f"z-frag-{index}", "page": index, "text": text}],
    )


class GroundedAI:
    def __init__(self, *, only_targeted=False, invent_conflict_quote=False):
        self.only_targeted = only_targeted
        self.invent_conflict_quote = invent_conflict_quote
        self.phases = []

    def complete_json(self, *, system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        if "obligation" in payload:
            obligation = payload["obligation"]["text"]
            selected = [item["link_id"] for item in payload["evidence"]
                        if ("КЛ-1" not in obligation or "КЛ-1" in item["znp_text"])
                        and ("КЛ-2" not in obligation or "КЛ-2" in item["znp_text"])]
            return {
                "covered": bool(selected),
                "evidence_ids": selected[:1],
                "object_consistent": bool(selected),
                "reasoning": "Доказательство проверено по объекту и характеристике",
            }
        if "candidates" in payload and "tu_candidates" not in payload:
            conflicts = []
            for candidate in payload["candidates"]:
                if "6 кВ" in candidate["text"]:
                    conflicts.append({
                        "atom_id": candidate["atom_id"],
                        "tu_quote": "10 кВ",
                        "znp_quote": ("несуществующая цитата" if self.invent_conflict_quote
                                      else "6 кВ"),
                        "reasoning": "Несовпадение напряжения для той же линии",
                    })
            return {"conflicts": conflicts, "uncertain_ids": []}
        self.phases.append(payload["phase"])
        atom = payload["znp_atom"]
        target = payload["tu_candidates"][0] if payload["tu_candidates"] else None
        if target is None or (self.only_targeted and payload["phase"] != "targeted"):
            return {"links": [], "search_more": False}
        if "6 кВ" in atom["text"]:
            return {"links": [], "search_more": False}
        return {"links": [{
            "requirement_id": target["requirement_id"],
            "relation": "supports",
            "tu_quote": target["text"],
            "znp_quote": atom["text"],
            "confidence": 0.94,
            "reasoning": "Прямое подтверждение",
        }], "search_more": False}


def agent(ai):
    return ZnPFirstAgent(ai_client=ai, embedder=EmbeddingService(enabled=False))


def test_register_keeps_full_condition_and_source():
    source = "1.1. Для КЛ-1 допускается кабель 95 мм² при условии двух линий."
    registry = build_obligations(req(source))
    assert len(registry) == 1
    assert registry[0].source_quote == source
    assert "при условии" in registry[0].context
    assert registry[0].requirement_id == "tu-1"
    assert registry[0].id == build_obligations(req(source))[0].id


def test_explicit_list_creates_individual_traceable_obligations():
    source = "1.1. Для линий предусмотреть:\n- КЛ-1 напряжение 10 кВ;\n- КЛ-2 напряжение 6 кВ."
    rows = build_obligations(req(source))
    assert len(rows) == 2
    assert all("Для линий" in row.context for row in rows)
    assert all(row.source_quote in source for row in rows)
    assert rows[0].id != rows[1].id


def test_targeted_second_pass_is_real_deepseek_analysis():
    ai = GroundedAI(only_targeted=True)
    result = agent(ai).verify_all(
        [req("1.1. Предусмотреть мощность 150 кВт.")],
        [znp(1, "2.1. Предусмотреть мощность 150 кВт.")],
    )
    item = result.checked_requirements[0]
    assert "targeted" in ai.phases
    assert item.validation_details["targeted_search"]
    assert item.validation_details["matches"]
    assert result.trace_info["targeted_model_calls"] >= 1
    assert item.status.value == "fulfilled"


def test_independent_contradiction_is_found_outside_primary_matches():
    ai = GroundedAI()
    result = agent(ai).verify_all(
        [req("1.1. Для КЛ-1 предусмотреть напряжение 10 кВ.")],
        [znp(1, "2.1. Для КЛ-1 предусмотреть напряжение 10 кВ."),
         znp(2, "2.2. Для КЛ-1 предусмотреть напряжение 6 кВ.")],
    )
    item = result.checked_requirements[0]
    assert item.status.value == "needs_review"
    audit = item.validation_details["contradiction_audit"]
    assert audit["candidates_checked"] >= 1
    assert audit["confirmed"]
    assert audit["confirmed"][0]["atom_id"] == "z-2"
    assert audit["confirmed"][0]["fragment_id"] == "z-frag-2"


def test_different_objects_not_mixed_in_independent_scan():
    assert scope_overlap("КЛ-1 напряжение 10 кВ", "КЛ-2 напряжение 6 кВ") is False
    result = agent(GroundedAI()).verify_all(
        [req("1.1. Для КЛ-1 предусмотреть напряжение 10 кВ.")],
        [znp(1, "2.1. Для КЛ-1 предусмотреть напряжение 10 кВ."),
         znp(2, "2.2. Для КЛ-2 предусмотреть напряжение 6 кВ.")],
    )
    item = result.checked_requirements[0]
    assert item.status.value == "fulfilled"
    assert item.validation_details["contradiction_audit"]["confirmed"] == []


def test_invalid_conflict_quote_cannot_become_evidence():
    result = agent(GroundedAI(invent_conflict_quote=True)).verify_all(
        [req("1.1. Для КЛ-1 предусмотреть напряжение 10 кВ.")],
        [znp(1, "2.1. Для КЛ-1 предусмотреть напряжение 10 кВ."),
         znp(2, "2.2. Для КЛ-1 предусмотреть напряжение 6 кВ.")],
    )
    item = result.checked_requirements[0]
    assert item.status.value == "needs_review"
    audit = item.validation_details["contradiction_audit"]
    assert not audit["confirmed"]
    assert audit["uncertain_atom_ids"] == ["z-2"]


def test_uncovered_list_obligation_stays_review_not_missing():
    text = "1.1. Предусмотреть:\n- КЛ-1 напряжение 10 кВ;\n- КЛ-2 напряжение 6 кВ."
    result = agent(GroundedAI()).verify_all(
        [req(text)], [znp(1, "2.1. КЛ-1 напряжение 10 кВ.")]
    )
    item = result.checked_requirements[0]
    assert item.status.value == "needs_review"
    assert len(item.validation_details["obligation_coverage"]) == 2
    assert any(v["status"] != "fulfilled" for v in item.validation_details["obligation_coverage"])
    assert item.validation_details["targeted_search"] == []


def test_model_cannot_confirm_other_object_through_similar_numeric_value():
    result = agent(GroundedAI()).verify_all(
        [req("1.1. Для КЛ-1 предусмотреть напряжение 10 кВ.")],
        [znp(1, "2.1. Для КЛ-2 предусмотреть напряжение 10 кВ.")],
    )
    item = result.checked_requirements[0]
    assert item.status.value == "needs_review"
