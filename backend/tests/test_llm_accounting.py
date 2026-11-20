"""Regression tests for paid-LLM accounting, source quotes and stop conditions."""
import json

import pytest

from app.ai.deepseek import DeepSeekError
from app.ai.embeddings import EmbeddingService
from app.requirements.llm_tracking import (
    LLMCallLimitExceeded, TrackedRequirementsLLM,
)
from app.requirements.schemas import RequirementCategory, TURequirement, ZNPClause
from app.requirements.znp_first import ZnPFirstAgent


def req():
    value = "1.1. Для КЛ-1 предусмотреть напряжение 10 кВ."
    return TURequirement(
        id="tu1", clause_number="1.1", category=RequirementCategory.voltage,
        text=value, fragment_id="t1", fragment_text=value,
        sources=[{"fragment_id": "t1", "text": value, "page": 1}],
    )


def atom(i):
    value = f"2.{i}. Для КЛ-1 предусмотреть напряжение 10 кВ."
    return ZNPClause(
        id=f"a{i}", clause_number=f"2.{i}", text=value,
        fragment_id=f"z{i}", fragment_text=value,
        sources=[{"fragment_id": f"z{i}", "text": value, "page": 2}],
    )


class Model:
    def __init__(self):
        self.calls = 0

    def complete_json(self, *, system_prompt, user_prompt):
        self.calls += 1
        payload = json.loads(user_prompt)
        if "obligation" in payload:
            return {
                "covered": True, "object_consistent": True,
                "evidence_ids": [item["link_id"] for item in payload["evidence"][:1]],
                "reasoning": "Совпадают объект и параметр",
            }
        if "candidates" in payload and "requirement" in payload:
            return {"conflicts": [], "uncertain_ids": []}
        if "znp_candidates" in payload:
            return {"links": []}
        candidate = payload["tu_candidates"][0]
        unit = payload["znp_atom"]
        assert unit["source_fragments"]
        assert candidate["source_fragments"]
        return {"links": [{
            "requirement_id": candidate["requirement_id"],
            "relation": "supports",
            "tu_quote": "напряжение 10 кВ",
            "znp_quote": "напряжение 10 кВ",
            "confidence": 0.92,
            "reasoning": "Исходные фрагменты совпадают",
        }], "search_more": False}


def agent(model):
    return ZnPFirstAgent(ai_client=model, embedder=EmbeddingService(enabled=False))


def test_full_pair_phase_budget_does_not_stop_at_small_document_defaults():
    class Counted:
        def complete_json(self, **kwargs):
            return {}

    ledger = TrackedRequirementsLLM(Counted(), max_calls=25000)
    ledger.configure_full_run_limits(
        atom_count=320, obligation_count=328,
        requirement_count=176, matching_batches=5,
    )
    for _ in range(121):
        ledger.complete_json(system_prompt="", user_prompt='{"znp_atom":{},"tu_candidates":[]}')
    for _ in range(66):
        ledger.complete_json(system_prompt="", user_prompt='{"obligation":{}}')

    assert ledger.attempted["matching"] == 121
    assert ledger.attempted["coverage"] == 66
    assert ledger.phase_limits["matching"] == 1600
    assert ledger.phase_limits["coverage"] == 656
    assert ledger.phase_limits["contradictions"] == 352
    assert ledger.phase_limits["targeted"] == 35


def test_late_provider_failure_does_not_relabel_successful_analysis_as_local():
    class FailsSecond(Model):
        def complete_json(self, *, system_prompt, user_prompt):
            payload = json.loads(user_prompt)
            if "znp_atom" in payload and self.calls >= 1:
                raise DeepSeekError("DeepSeek request timed out.")
            return super().complete_json(
                system_prompt=system_prompt, user_prompt=user_prompt,
            )

    result = agent(FailsSecond()).verify_all([req()], [atom(1), atom(2)])
    check = result.checked_requirements[0]
    assert check.method == "llm"
    assert check.status.value == "needs_review"
    assert result.trace_info["ai_evaluated_requirements"] == 1
    assert result.trace_info["validated_links"] == 1
    assert result.trace_info["degraded"] is True


def test_unchecked_coverage_marks_full_comparison_degraded():
    class CoverageFails(Model):
        def complete_json(self, *, system_prompt, user_prompt):
            if "obligation" in json.loads(user_prompt):
                raise DeepSeekError("DeepSeek request timed out.")
            return super().complete_json(
                system_prompt=system_prompt, user_prompt=user_prompt,
            )

    result = agent(CoverageFails()).verify_all([req()], [atom(1)])

    assert result.trace_info["comparison_stages"]["coverage"]["unreviewed"] == 1
    assert result.trace_info["degraded"] is True


def test_valid_model_no_links_is_not_fallback_or_positive():
    class Empty(Model):
        def complete_json(self, *, system_prompt, user_prompt):
            payload = json.loads(user_prompt)
            if "znp_atom" in payload:
                return {"links": [], "search_more": False}
            return super().complete_json(
                system_prompt=system_prompt, user_prompt=user_prompt,
            )

    result = agent(Empty()).verify_all([req()], [atom(1)])
    check = result.checked_requirements[0]
    assert check.method == "llm"
    assert check.status.value == "needs_review"
    assert result.trace_info["validated_links"] == 0
    assert result.trace_info["ai_evaluated_requirements"] == 1


def test_short_quote_from_original_source_passes_for_composed_atom():
    source = "2.1. Для КЛ-1 предусмотреть алюминиевый кабель на напряжение 10 кВ."
    constructed = "2.1. Для КЛ-1 предусмотреть\nнапряжение 10 кВ"
    clause = ZNPClause(
        id="a1", clause_number="2.1", text=constructed,
        fragment_id="z1", fragment_text=source,
        sources=[{"fragment_id": "z1", "text": source, "page": 2}],
    )
    result = agent(Model()).verify_all([req()], [clause])
    assert result.trace_info["validated_links"] == 1
    assert result.checked_requirements[0].method == "llm"


def test_repeated_nonexistent_quotes_stop_more_paid_calls():
    class Invented(Model):
        def complete_json(self, *, system_prompt, user_prompt):
            payload = json.loads(user_prompt)
            if "znp_atom" in payload:
                self.calls += 1
                candidate = payload["tu_candidates"][0]
                return {"links": [{
                    "requirement_id": candidate["requirement_id"],
                    "relation": "supports",
                    "tu_quote": "напряжение 10 кВ",
                    "znp_quote": "Выдуманная цитата 10 кВ",
                    "confidence": 1.0,
                }], "search_more": False}
            return super().complete_json(
                system_prompt=system_prompt, user_prompt=user_prompt,
            )

    ai = Invented()
    result = agent(ai).verify_all([req()], [atom(i) for i in range(1, 40)])
    assert result.trace_info["stopped_due_to_invalid_evidence"] is True
    assert ai.calls == 12
    assert result.trace_info["validated_links"] == 0
    assert result.trace_info["rejected_links"]["znp_quote_not_in_atom"] == 12
    assert result.checked_requirements[0].status.value == "needs_review"


def test_budget_shared_across_phases_and_does_not_call_model_when_exhausted():
    class Echo:
        calls = 0

        def complete_json(self, **kwargs):
            self.calls += 1
            return {"ok": True}

    underlying = Echo()
    ledger = TrackedRequirementsLLM(
        underlying, max_calls=2, atomization_limit=1, targeted_limit=0,
    )
    assert ledger.complete_json(
        system_prompt="test", user_prompt='{"clause_text":"x"}'
    ) == {"ok": True}
    assert ledger.complete_json(
        system_prompt="test",
        user_prompt='{"znp_atom":{"text":"x"},"phase":"primary"}',
    ) == {"ok": True}
    with pytest.raises(LLMCallLimitExceeded):
        ledger.complete_json(
            system_prompt="test", user_prompt='{"clause_text":"y"}',
        )
    assert underlying.calls == 2
    assert ledger.trace()["model_calls_total"] == 2
    assert ledger.trace()["limit_reached"]


def test_targeted_phase_limit_does_not_spend_additional_calls():
    class Echo:
        calls = 0

        def complete_json(self, **kwargs):
            self.calls += 1
            return {"links": []}

    underlying = Echo()
    ledger = TrackedRequirementsLLM(
        underlying, max_calls=5, atomization_limit=0, targeted_limit=1,
    )
    payload = '{"phase":"targeted","znp_atom":{"id":"x"}}'
    ledger.complete_json(system_prompt="test", user_prompt=payload)
    with pytest.raises(LLMCallLimitExceeded) as exc:
        ledger.complete_json(system_prompt="test", user_prompt=payload)
    assert exc.value.scope == "phase"
    assert underlying.calls == 1
