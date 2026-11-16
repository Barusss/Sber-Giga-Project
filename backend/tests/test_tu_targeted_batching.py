"""Regression checks for batched TU->ZnP fallback and truthful diagnostics."""
import json

from app.ai.embeddings import EmbeddingService
from app.requirements.llm_tracking import TrackedRequirementsLLM
from app.requirements.schemas import RequirementCategory, TURequirement, ZNPClause
from app.requirements.znp_first import ZnPFirstAgent


def requirement(index):
    text = f"{index}. Для КЛ-1 предусмотреть напряжение 10 кВ."
    return TURequirement(
        id=f"tu{index}", clause_number=str(index),
        category=RequirementCategory.voltage, text=text,
        fragment_id=f"t{index}", fragment_text=text,
        sources=[{"fragment_id": f"t{index}", "text": text, "page": 1}],
    )


def atom(index):
    text = f"2.{index}. Для КЛ-1 предусмотреть напряжение 10 кВ."
    return ZNPClause(
        id=f"a{index}", clause_number=f"2.{index}", text=text,
        fragment_id=f"z{index}", fragment_text=text,
        sources=[{"fragment_id": f"z{index}", "text": text, "page": 2}],
    )


class NoLinks:
    def __init__(self):
        self.phases = []

    def complete_json(self, *, system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        self.phases.append(payload.get("phase", "primary"))
        if "znp_candidates" in payload:
            assert payload["tu_requirement"]["id"].startswith("tu")
            assert len(payload["znp_candidates"]) > 1
            return {"links": []}
        if "obligation" in payload:
            return {
                "covered": False, "object_consistent": False,
                "evidence_ids": [], "reasoning": "Нет связей",
            }
        if "candidates" in payload and "requirement" in payload:
            return {"conflicts": [], "uncertain_ids": []}
        return {"links": [], "search_more": False}


def test_targeted_one_batch_per_tu_not_one_request_per_pair():
    model = NoLinks()
    ledger = TrackedRequirementsLLM(model, max_calls=50, targeted_limit=10)
    agent = ZnPFirstAgent(
        ai_client=ledger, embedder=EmbeddingService(enabled=False),
        batch_size=3, max_batches=1,
    )
    result = agent.verify_all([requirement(1)], [atom(i) for i in range(1, 5)])
    assert ledger.trace()["attempted_by_phase"]["targeted"] == 1
    assert model.phases.count("targeted") == 1
    details = result.checked_requirements[0].validation_details
    assert details["targeted_diagnostics"]["checked"] is True
    assert details["targeted_diagnostics"]["candidates"] > 1
    assert details["primary_candidate_evaluations"] > 0
    assert result.checked_requirements[0].status.value == "needs_review"


def test_targeted_limit_keeps_remaining_tu_needing_review():
    model = NoLinks()
    ledger = TrackedRequirementsLLM(model, max_calls=50, targeted_limit=1)
    agent = ZnPFirstAgent(
        ai_client=ledger, embedder=EmbeddingService(enabled=False),
        batch_size=3, max_batches=1,
    )
    result = agent.verify_all(
        [requirement(1), requirement(2)], [atom(i) for i in range(1, 5)]
    )
    assert ledger.trace()["attempted_by_phase"]["targeted"] == 1
    assert result.trace_info["targeted_limit_reached"] is True
    second = result.checked_requirements[1]
    assert second.validation_details["targeted_diagnostics"]["skip_reason"] == "limit"
    assert second.status.value == "needs_review"


def test_batched_targeted_phase_is_accounted_for_in_its_own_budget():
    model = NoLinks()
    ledger = TrackedRequirementsLLM(model, max_calls=3, targeted_limit=0)
    response = '{"phase":"targeted","tu_requirement":{"id":"x"},"znp_candidates":[]}'
    assert TrackedRequirementsLLM.phase(response) == "targeted"
    assert ledger.phase_exhausted("targeted") is True
