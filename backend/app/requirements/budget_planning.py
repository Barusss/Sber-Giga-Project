"""No-API full-comparison request budget planning.

This is an admission guard for a known under-sized budget, NOT a prediction
of actual API usage, money, or matching quality. It never raises limits.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from app.requirements.coverage import build_obligations
from app.requirements.schemas import TURequirement
from app.requirements.tools import classify_requirement_category


@dataclass(frozen=True)
class FullRunBudgetPlan:
    tu_requirements: int
    znp_source_atoms: int
    obligations: int
    atomization_eligible: int
    max_calls: int
    atomization_limit: int
    targeted_limit: int
    matching_batches: int
    targeted_enabled: bool

    @property
    def initial_matching_baseline(self) -> int:
        # Assumption: each original ZnP atom has at least one TU candidate,
        # requiring one mandatory matching call. Splitting can increase this.
        return self.znp_source_atoms

    @property
    def first_pass_baseline(self) -> int:
        return self.atomization_eligible + self.initial_matching_baseline

    @property
    def blockers(self) -> list[str]:
        errors = []
        if self.atomization_limit < self.atomization_eligible:
            errors.append(
                "atomization_cap_below_eligible:"
                f"{self.atomization_limit}<{self.atomization_eligible}"
            )
        if self.max_calls < self.first_pass_baseline:
            errors.append(
                "global_cap_below_first_pass_baseline:"
                f"{self.max_calls}<{self.first_pass_baseline}"
            )
        return errors

    @property
    def warnings(self) -> list[str]:
        warnings = []
        if self.targeted_enabled and self.targeted_limit < self.tu_requirements:
            warnings.append(
                "targeted_cap_may_limit_reverse_search:"
                f"{self.targeted_limit}<{self.tu_requirements}"
            )
        if self.max_calls < self.first_pass_baseline + self.obligations:
            warnings.append(
                "global_cap_may_limit_coverage:"
                f"{self.max_calls}<{self.first_pass_baseline + self.obligations}"
            )
        return warnings

    def as_dict(self) -> dict:
        return {
            **asdict(self),
            "initial_matching_baseline": self.initial_matching_baseline,
            "first_pass_baseline": self.first_pass_baseline,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "assumptions": (
                "Baseline assumes one matching request per unsplit source atom "
                "and all eligible atomization attempts. Actual calls vary with "
                "retrieval candidates, splitting, coverage, targeted and retries."
            ),
        }

    def refusal_message(self) -> str:
        return (
            "Full TU/ZnP run blocked before paid DeepSeek calls: "
            + ", ".join(self.blockers)
            + ". These are planning assumptions, not exact request counts. "
            + "Review REQUIREMENTS_LLM_MAX_CALLS and "
            + "REQUIREMENTS_LLM_ATOMIZATION_CALLS; changes require an "
            + "explicitly accepted paid-request budget. "
            + "REQUIREMENTS_LLM_TARGETED_CALLS may also limit reverse search. "
            + "No settings were raised automatically."
        )


def plan_full_run(
    *, tu_clauses, znp_source_atoms: int, atomization_eligible: int,
    max_calls: int, atomization_limit: int, targeted_limit: int,
    matching_batches: int, targeted_enabled: bool = True,
) -> FullRunBudgetPlan:
    obligations = 0
    for index, clause in enumerate(tu_clauses, start=1):
        requirement = TURequirement(
            id=f"budget-{index}",
            clause_number=clause.clause_number,
            category=classify_requirement_category(clause.text),
            text=clause.text,
        )
        obligations += len(build_obligations(requirement))
    return FullRunBudgetPlan(
        tu_requirements=len(tu_clauses),
        znp_source_atoms=znp_source_atoms,
        obligations=obligations,
        atomization_eligible=atomization_eligible,
        max_calls=max_calls,
        atomization_limit=atomization_limit,
        targeted_limit=targeted_limit,
        matching_batches=matching_batches,
        targeted_enabled=targeted_enabled,
    )
