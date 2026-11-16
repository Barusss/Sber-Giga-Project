"""Regression tests for ZnP-first orientation, request budgets and conflict triage."""
import json

from app.ai.embeddings import EmbeddingService
from app.requirements.llm_tracking import TrackedRequirementsLLM
from app.requirements.schemas import RequirementCategory, TURequirement, ZNPClause
from app.requirements.znp_first import ZnPFirstAgent


def requirement(number=1, power=150):
    value = f"{number}. Для КЛ-1 предусмотреть мощность {power} кВт."
    return TURequirement(
        id=f"tu-{number}", clause_number=str(number),
        category=RequirementCategory.other, text=value,
        fragment_id=f"tu-f{number}", fragment_text=value,
    )


def znp(number=1, power=150):
    value = f"{number}. Для КЛ-1 предусмотреть мощность {power} кВт."
    return ZNPClause(
        id=f"znp-{number}", clause_number=str(number), text=value,
        fragment_id=f"znp-f{number}", fragment_text=value,
        sources=[{"fragment_id": f"znp-f{number}", "page": 1, "text": value}],
    )


class MatchModel:
    def __init__(self, *, empty=False):
        self.match_calls = 0
        self.empty = empty

    def complete_json(self, *, system_prompt, user_prompt):
        request = json.loads(user_prompt)
        if "obligation" in request:
            return {
                "covered": True,
                "object_consistent": True,
                "evidence_ids": [value["link_id"] for value in request["evidence"][:1]],
            }
        if "candidates" in request and "requirement" in request:
            return {"conflicts": [], "uncertain_ids": []}
        self.match_calls += 1
        if self.empty:
            return {"links": [], "search_more": False}
        atom = request["znp_atom"]
        requirement = request["tu_candidates"][0]
        return {"links": [{
            "requirement_id": requirement["requirement_id"],
            "relation": "supports",
            "tu_quote": "Для КЛ-1 предусмотреть мощность 150 кВт.",
            "znp_quote": "Для КЛ-1 предусмотреть мощность 150 кВт.",
            "confidence": 0.95,
        }], "search_more": False}


def compare(tu, atoms, ai):
    return ZnPFirstAgent(
        ai_client=ai, embedder=EmbeddingService(enabled=False)
    ).verify_all(tu, atoms)


def test_primary_output_contains_znp_atoms_with_links_to_tu():
    result = compare([requirement()], [znp(1), znp(2)], MatchModel())
    atoms = result.trace_info["znp_atom_results"]
    assert len(atoms) == 2
    assert {item["atom_id"] for item in atoms} == {"znp-1", "znp-2"}
    assert all(item["model_checked"] for item in atoms)
    assert all(item["links"][0]["tu_requirement_id"] == "tu-1" for item in atoms)
    assert all(item["links"][0]["tu_clause_number"] == "1" for item in atoms)
    assert all(item["links"][0]["confidence"] == 0.95 for item in atoms)
    assert all(isinstance(item["links"][0]["search_score"], float) for item in atoms)
    assert result.trace_info["validated_links"] == 2


def test_no_links_model_response_does_not_trigger_extra_candidate_pages():
    ai = MatchModel(empty=True)
    ledger = TrackedRequirementsLLM(
        ai, max_calls=15, atomization_limit=0, targeted_limit=0,
        matching_limit=10, coverage_limit=5, contradictions_limit=5,
    )
    result = compare([requirement(n) for n in range(1, 25)], [znp(1)], ledger)
    assert ai.match_calls == 1
    assert result.trace_info["znp_atom_results"][0]["model_checked"] is True
    assert result.trace_info["znp_atom_results"][0]["links"] == []
    assert result.trace_info["validated_links"] == 0


def test_uncovered_tu_does_not_create_mass_unchecked_conflicts():
    result = compare(
        [requirement()], [znp(1, 180), znp(2, 200)], MatchModel(empty=True)
    )
    item = result.checked_requirements[0]
    audit = item.validation_details["contradiction_audit"]
    assert item.status.value == "needs_review"
    assert audit["skipped_uncovered"] is True
    assert audit["candidate_count"] == 0
    assert audit["unreviewed_count"] == 0
    assert "непроверенные кандидаты" not in item.reasoning.lower()


def test_matching_phase_limit_preserves_coverage_budget_and_reports_unseen_atoms():
    ai = MatchModel()
    ledger = TrackedRequirementsLLM(
        ai, max_calls=12, atomization_limit=0, targeted_limit=0,
        matching_limit=1, coverage_limit=5, contradictions_limit=5,
    )
    result = compare([requirement()], [znp(1), znp(2)], ledger)
    trace = result.trace_info
    assert ai.match_calls == 1
    assert trace["matching_limit_reached"] is True
    assert trace["znp_atom_results"][0]["model_checked"] is True
    assert trace["znp_atom_results"][1]["model_checked"] is False
    assert ledger.attempted["coverage"] >= 1
    assert ledger.total <= 12
    assert trace["validated_links"] == 1



def test_model_confidence_missing_is_not_invented_as_zero_in_znp_view():
    class Unrated(MatchModel):
        def complete_json(self, *, system_prompt, user_prompt):
            response = super().complete_json(
                system_prompt=system_prompt, user_prompt=user_prompt,
            )
            if "links" in response:
                for link in response["links"]:
                    link.pop("confidence", None)
            return response

    result = compare([requirement()], [znp(1)], Unrated())
    link = result.trace_info["znp_atom_results"][0]["links"][0]
    assert link["confidence"] is None
    assert link["relation"] == "partial"
    assert result.checked_requirements[0].validation_details["matches"][0][
        "model_confidence_available"
    ] is False


def test_invalid_nonfinite_confidence_is_not_displayed_as_a_percentage():
    class InvalidScore(MatchModel):
        def complete_json(self, *, system_prompt, user_prompt):
            response = super().complete_json(
                system_prompt=system_prompt, user_prompt=user_prompt,
            )
            if "links" in response:
                response["links"][0]["confidence"] = "nan"
            return response

    result = compare([requirement()], [znp(1)], InvalidScore())
    assert result.trace_info["znp_atom_results"][0]["links"][0]["confidence"] is None



def test_preview_pipeline_never_calls_reverse_targeted_matching():
    class RecordingModel(MatchModel):
        def __init__(self):
            super().__init__(empty=True)
            self.phases = []

        def complete_json(self, *, system_prompt, user_prompt):
            payload = json.loads(user_prompt)
            self.phases.append(payload.get("phase"))
            if payload.get("phase") == "targeted":
                raise AssertionError("Preview must not pay for TU -> ZnP search")
            return super().complete_json(
                system_prompt=system_prompt, user_prompt=user_prompt,
            )

    model = RecordingModel()
    agent = ZnPFirstAgent(
        ai_client=model,
        embedder=EmbeddingService(enabled=False),
        batch_size=2,
        max_batches=1,
        targeted_enabled=False,
    )
    result = agent.verify_all(
        [requirement()], [znp(1), znp(2)],
    )
    stages = result.trace_info["comparison_stages"]
    assert stages["matching"]["total"] == 2
    assert stages["matching"]["checked"] == 2
    assert stages["matching"]["validated_links"] == 0
    assert stages["targeted"] == {
        "enabled": False, "attempted": 0, "limit_reached": False,
    }
    assert result.trace_info["targeted_model_calls"] == 0
    assert all(entry["model_checked"] for entry in result.trace_info["znp_atom_results"])
    assert all(entry["links"] == [] for entry in result.trace_info["znp_atom_results"])


def test_coverage_budget_incomplete_does_not_change_primary_match_status():
    model = MatchModel()
    ledger = TrackedRequirementsLLM(
        model, max_calls=6, atomization_limit=0,
        matching_limit=2, coverage_limit=0,
        targeted_limit=0, contradictions_limit=0,
    )
    agent = ZnPFirstAgent(
        ai_client=ledger, embedder=EmbeddingService(enabled=False),
        targeted_enabled=False,
    )
    result = agent.verify_all([requirement()], [znp(1)])
    stages = result.trace_info["comparison_stages"]
    assert stages["matching"]["checked"] == 1
    assert stages["matching"]["validated_links"] == 1
    assert stages["coverage"]["total"] >= 1
    assert stages["coverage"]["unreviewed"] >= 1
    assert stages["coverage"]["fulfilled"] == 0
    assert stages["targeted"]["enabled"] is False
    assert ledger.attempted["targeted"] == 0
    assert result.checked_requirements[0].status.value == "needs_review"
    assert result.trace_info["znp_atom_results"][0]["model_checked"] is True


def test_reverse_links_keep_origin_separate_from_primary_search():
    model = MatchModel()
    result = compare([requirement()], [znp(1)], model)
    links = result.trace_info["znp_atom_results"][0]["links"]
    assert any(link["source_phase"] == "matching" for link in links)
