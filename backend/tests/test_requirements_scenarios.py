"""Regression tests over the committed TU/ZnP manual-test scenarios."""
import json
from pathlib import Path

import pytest

from app.ai.deepseek import DeepSeekError
from app.ai.embeddings import EmbeddingService
from app.requirements.agent import RequirementsAgent
from app.requirements.schemas import TURequirement, ZNPClause

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_FILES = (
    ROOT / "docs" / "manual-test" / "tu-znp" / "scenarios.json",
    ROOT / "docs" / "manual-test" / "real-tu-2250" / "scenarios.json",
)


class OfflineAI:
    def complete_json(self, **kwargs):
        raise DeepSeekError("offline scenario test")


def _load_scenarios():
    scenarios = []
    for path in SCENARIO_FILES:
        for name, scenario in json.loads(path.read_text(encoding="utf-8")).items():
            scenarios.append(pytest.param(scenario, id=name))
    return scenarios


@pytest.mark.parametrize("scenario", _load_scenarios())
def test_deterministic_fallback_matches_manual_scenario(scenario):
    payload = scenario["payload"]
    requirements = [TURequirement(**item) for item in payload["tu_requirements"]]
    clauses = [ZNPClause(**item) for item in payload["znp_clauses"]]
    agent = RequirementsAgent(
        ai_client=OfflineAI(),
        embedder=EmbeddingService(enabled=False),
    )

    result = agent.verify_all(requirements, clauses)
    actual = {
        item.requirement.id: item.status.value
        for item in result.checked_requirements
    }

    assert actual == scenario["expected"]["statuses"]
    assert result.stats == scenario["expected"]["stats"]
