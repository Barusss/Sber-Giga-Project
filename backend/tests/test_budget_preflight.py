"""Offline full-comparison budgets never authorize paid calls or silently grow caps."""
from app.requirements.budget_planning import FullRunBudgetPlan, plan_full_run
from app.requirements.extraction import Clause
from app.requirements.semantic_atomization import atomization_workload


def test_original_pair_configuration_is_refused_before_any_paid_request():
    plan = FullRunBudgetPlan(
        tu_requirements=176, znp_source_atoms=320, obligations=328,
        atomization_eligible=57, max_calls=250,
        atomization_limit=40, targeted_limit=35,
        matching_batches=5, targeted_enabled=True,
    )
    assert plan.first_pass_baseline == 377
    assert plan.blockers == [
        "atomization_cap_below_eligible:40<57",
        "global_cap_below_first_pass_baseline:250<377",
    ]
    assert "targeted_cap_may_limit_reverse_search:35<176" in plan.warnings
    assert "No settings were raised automatically" in plan.refusal_message()


def test_approved_larger_caps_allow_attempt_but_do_not_guarantee_completion():
    plan = FullRunBudgetPlan(
        tu_requirements=176, znp_source_atoms=320, obligations=328,
        atomization_eligible=57, max_calls=1000,
        atomization_limit=80, targeted_limit=200,
        matching_batches=5, targeted_enabled=True,
    )
    assert plan.blockers == []
    assert plan.as_dict()["initial_matching_baseline"] == 320
    assert "Actual calls vary" in plan.as_dict()["assumptions"]


def test_targeted_warning_does_not_block_fully_matched_small_pairs():
    plan = FullRunBudgetPlan(
        tu_requirements=2, znp_source_atoms=1, obligations=4,
        atomization_eligible=0, max_calls=10,
        atomization_limit=0, targeted_limit=0,
        matching_batches=5, targeted_enabled=True,
    )
    assert plan.blockers == []
    assert "targeted_cap_may_limit_reverse_search:0<2" in plan.warnings


def test_planning_uses_actual_coverage_splitting_not_guessed_items():
    clauses = [
        Clause(
            clause_number="1", text="Для КЛ-1 предусмотреть 10 кВ.",
            page=1, fragment_id=None, fragment_ids=[],
            section_path=None, fragment_text="Для КЛ-1 предусмотреть 10 кВ.",
        ),
    ]
    plan = plan_full_run(
        tu_clauses=clauses,
        znp_source_atoms=320,
        atomization_eligible=57,
        max_calls=250,
        atomization_limit=40,
        targeted_limit=35,
        matching_batches=5,
    )
    assert plan.tu_requirements == 1
    assert plan.obligations == 1
    assert plan.blockers


def test_atomization_workload_counts_every_source_atom_without_model_calls():
    from app.requirements.atomization import DocumentAtom
    atom = DocumentAtom(
        id="one", document_id="d", clause_number="1",
        text="Для КЛ-1 предусмотреть кабель 10 кВ.",
        section_path=None, sources=(), context_sources=(), warnings=(),
    )
    warned = DocumentAtom(
        id="two", document_id="d", clause_number="2",
        text="Для КЛ-2 предусмотреть кабель 10 кВ.",
        section_path=None, sources=(), context_sources=(),
        warnings=("context_scope_requires_review",),
    )
    stats = atomization_workload([atom, warned])
    assert stats["total"] == 2
    assert stats["ambiguous_source_context"] == 1
    assert stats["eligible"] + sum(
        value for key, value in stats.items() if key not in {"eligible", "total"}
    ) == 2
