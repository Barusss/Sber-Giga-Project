"""Behavioral checks for the workspace's conservative ZnP-first comparison."""
import json

from app.ai.deepseek import DeepSeekError
from app.ai.embeddings import EmbeddingService
from app.requirements.schemas import TURequirement, ZNPClause, RequirementCategory
from app.requirements.znp_first import ZnPFirstAgent


def requirement(text="1.1. Предусмотреть мощность 150 кВт."):
    return TURequirement(
        id="tu-1", clause_number="1.1", category=RequirementCategory.power,
        text=text, fragment_id="t1", fragment_text=text,
    )


def atom(i, text="2.1. Предусмотреть мощность 150 кВт."):
    return ZNPClause(
        id=f"znp-{i}", clause_number=str(i), text=text,
        fragment_id=f"f{i}", fragment_text=text,
        sources=[{"fragment_id": f"f{i}", "page": i, "text": text}],
    )


class PositiveAI:
    def complete_json(self, *, system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        znp = payload["znp_atom"]
        candidates = payload["tu_candidates"]
        target = next((item for item in candidates if item["requirement_id"] == "tu-1"), None)
        if target is None:
            return {"links": [], "search_more": True}
        return {"links": [{
            "requirement_id": "tu-1",
            "relation": "supports",
            "tu_quote": target["text"],
            "znp_quote": znp["text"],
            "confidence": 0.93,
            "reasoning": "Требование подтверждается дословным фрагментом",
        }], "search_more": False}


class OfflineAI:
    def complete_json(self, **kwargs):
        raise DeepSeekError("No connection")


def test_multiple_znp_atoms_are_all_linked_to_one_tu():
    agent = ZnPFirstAgent(ai_client=PositiveAI(),
                          embedder=EmbeddingService(enabled=False))
    result = agent.verify_all([requirement()], [atom(i) for i in range(1, 21)])
    item = result.checked_requirements[0]
    assert item.status.value == "fulfilled"
    assert len(item.validation_details["matches"]) == 20
    assert len({match["atom_id"] for match in item.validation_details["matches"]}) == 20
    assert result.trace_info["znp_atoms_processed"] == 20


def test_hundred_znp_atoms_are_processed_without_a_global_top_three_limit():
    result = ZnPFirstAgent(ai_client=PositiveAI(),
                           embedder=EmbeddingService(enabled=False)).verify_all(
        [requirement()], [atom(i) for i in range(1, 101)]
    )
    assert result.trace_info["znp_atoms_processed"] == 100
    assert len(result.checked_requirements[0].validation_details["matches"]) == 100


def test_conflicting_parameter_never_returns_automatic_fulfilled():
    class ConflictAI(PositiveAI):
        def complete_json(self, *, system_prompt, user_prompt):
            answer = super().complete_json(system_prompt=system_prompt, user_prompt=user_prompt)
            answer["links"][0]["relation"] = "conflicts"
            return answer
    result = ZnPFirstAgent(ai_client=ConflictAI(),
                           embedder=EmbeddingService(enabled=False)).verify_all(
        [requirement()], [atom(1, "2.1. Предусмотреть мощность 200 кВт.")]
    )
    item = result.checked_requirements[0]
    assert item.status.value == "needs_review"
    assert item.validation_details["matches"][0]["relation"] == "conflicts"


def test_offline_mode_does_not_claim_absence_or_compliance():
    result = ZnPFirstAgent(ai_client=OfflineAI(),
                           embedder=EmbeddingService(enabled=False)).verify_all(
        [requirement()], [atom(1)]
    )
    item = result.checked_requirements[0]
    assert item.status.value == "needs_review"
    assert not item.validation_details["matches"]
    assert result.trace_info["degraded"] is True
    assert result.trace_info["agent_runs"][0]["steps"][0]["tool"] == "search_tu"


def test_cross_fragment_quote_cannot_confirm():
    first = ZNPClause(
        id="znp-1", clause_number="2.1",
        text="2.1. Предусмотреть мощность 150 кВт.",
        fragment_id="f1", fragment_text="2.1. Предусмотреть",
        sources=[
            {"fragment_id": "f1", "page": 1, "text": "2.1. Предусмотреть"},
            {"fragment_id": "f2", "page": 2, "text": "мощность 150 кВт."},
        ],
    )
    result = ZnPFirstAgent(ai_client=PositiveAI(),
                           embedder=EmbeddingService(enabled=False)).verify_all(
        [requirement()], [first]
    )
    assert result.checked_requirements[0].status.value == "needs_review"
    assert result.checked_requirements[0].validation_details["matches"] == []


def test_ambiguous_atom_warning_never_confirms():
    suspect = atom(1)
    suspect.warnings.append("context_scope_requires_review")
    result = ZnPFirstAgent(ai_client=PositiveAI(),
                           embedder=EmbeddingService(enabled=False)).verify_all(
        [requirement()], [suspect]
    )
    assert result.checked_requirements[0].status.value == "needs_review"
    assert result.checked_requirements[0].validation_details["matches"][0]["relation"] == "partial"
