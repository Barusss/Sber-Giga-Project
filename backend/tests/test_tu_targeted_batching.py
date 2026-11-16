"""Regression checks for batched TU->ZnP fallback and truthful diagnostics."""
import json

from app.ai.deepseek import DeepSeekResponseError
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


class TruncateLarge(NoLinks):
    def complete_json(self, *, system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        candidates = payload.get("znp_candidates")
        if candidates is not None:
            if len(candidates) > 2:
                raise DeepSeekResponseError(
                    "DeepSeek response was truncated; increase DEEPSEEK_MAX_TOKENS."
                )
            return {"links": []}
        return super().complete_json(
            system_prompt=system_prompt, user_prompt=user_prompt,
        )


def test_truncated_targeted_batch_retries_with_smaller_batches():
    ledger = TrackedRequirementsLLM(TruncateLarge(), max_calls=50, targeted_limit=10)
    agent = ZnPFirstAgent(
        ai_client=ledger, embedder=EmbeddingService(enabled=False),
        batch_size=3, max_batches=1,
    )
    result = agent.verify_all([requirement(1)], [atom(i) for i in range(1, 5)])
    details = result.checked_requirements[0].validation_details["targeted_diagnostics"]
    assert ledger.trace()["attempted_by_phase"]["targeted"] == 3
    assert ledger.trace()["responses_by_phase"]["targeted"] == 2
    assert details["checked"] is True
    assert details["checked_candidates"] == 4
    assert details["unchecked_candidates"] == 0
    assert details["truncated_responses"] == 1
    assert result.trace_info["targeted_truncated_responses"] == 1
    assert result.trace_info["model_failure_kind"] is None
    assert result.trace_info["degraded"] is False


class TruncateOne(NoLinks):
    def complete_json(self, *, system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        candidates = payload.get("znp_candidates")
        if candidates is not None:
            if len(candidates) > 2 or any(
                row["atom_id"] == "a1" for row in candidates
            ):
                raise DeepSeekResponseError(
                    "DeepSeek response was truncated; increase DEEPSEEK_MAX_TOKENS."
                )
            return {"links": []}
        return super().complete_json(
            system_prompt=system_prompt, user_prompt=user_prompt,
        )


def test_single_candidate_truncation_is_incomplete_not_a_global_failure():
    ledger = TrackedRequirementsLLM(TruncateOne(), max_calls=50, targeted_limit=15)
    agent = ZnPFirstAgent(
        ai_client=ledger, embedder=EmbeddingService(enabled=False),
        batch_size=3, max_batches=1,
    )
    result = agent.verify_all([requirement(1)], [atom(i) for i in range(1, 5)])
    item = result.checked_requirements[0]
    details = item.validation_details["targeted_diagnostics"]
    assert details["checked"] is False
    assert details["checked_candidates"] == 3
    assert details["unchecked_candidates"] == 1
    assert details["skip_reason"] == "truncated"
    assert result.trace_info["targeted_incomplete_requirements"] == 1
    assert result.trace_info["model_failure_kind"] is None
    assert result.trace_info["degraded"] is True
    assert item.status.value == "needs_review"


def test_truncated_split_respects_targeted_phase_limit():
    ledger = TrackedRequirementsLLM(TruncateLarge(), max_calls=50, targeted_limit=1)
    agent = ZnPFirstAgent(
        ai_client=ledger, embedder=EmbeddingService(enabled=False),
        batch_size=3, max_batches=1,
    )
    result = agent.verify_all([requirement(1)], [atom(i) for i in range(1, 5)])
    details = result.checked_requirements[0].validation_details["targeted_diagnostics"]
    assert ledger.trace()["attempted_by_phase"]["targeted"] == 1
    assert details["checked"] is False
    assert details["unchecked_candidates"] == 4
    assert details["skip_reason"] == "limit"
    assert result.trace_info["targeted_limit_reached"] is True
    assert result.trace_info["degraded"] is True


class InvalidResponse(NoLinks):
    def complete_json(self, *, system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        if "znp_candidates" in payload:
            raise DeepSeekResponseError("DeepSeek returned invalid JSON.")
        return super().complete_json(
            system_prompt=system_prompt, user_prompt=user_prompt,
        )


def test_non_truncation_response_error_stays_fatal():
    ledger = TrackedRequirementsLLM(InvalidResponse(), max_calls=50)
    agent = ZnPFirstAgent(
        ai_client=ledger, embedder=EmbeddingService(enabled=False),
        batch_size=3, max_batches=1,
    )
    result = agent.verify_all([requirement(1)], [atom(i) for i in range(1, 5)])
    assert result.trace_info["model_failure_kind"] == "DeepSeekResponseError"
    assert "invalid JSON" in result.trace_info["model_failure_reason"]
