# -*- coding: utf-8 -*-
"""Offline self-check for the real TU + synthetic ZnP scenario.

Runs the real RequirementsAgent with a forced LLM failure, so the deterministic
fallback is exercised without API keys or network access.
"""
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from app.ai.deepseek import DeepSeekError
from app.ai.embeddings import EmbeddingService
from app.requirements.agent import RequirementsAgent
from app.requirements.schemas import TURequirement, ZNPClause


class OfflineAI:
    def complete_json(self, **kwargs):
        raise DeepSeekError("offline real TU test")


def main() -> int:
    scenarios_path = Path(__file__).with_name("scenarios.json")
    scenarios = json.loads(scenarios_path.read_text(encoding="utf-8"))
    failures = 0

    for name, scenario in scenarios.items():
        payload = scenario["payload"]
        tu = [TURequirement(**item) for item in payload["tu_requirements"]]
        znp = [ZNPClause(**item) for item in payload["znp_clauses"]]

        agent = RequirementsAgent(
            ai_client=OfflineAI(),
            embedder=EmbeddingService(enabled=False),
        )
        result = agent.verify_all(tu, znp)
        actual = {
            item.requirement.id: item.status.value
            for item in result.checked_requirements
        }
        expected = scenario["expected"]["statuses"]

        print(f"\n{name}")
        print(f"  {scenario['description']}")
        if scenario.get("note"):
            print(f"  note: {scenario['note']}")

        for requirement_id, expected_status in expected.items():
            actual_status = actual.get(requirement_id, "MISSING_FROM_RESULT")
            ok = actual_status == expected_status
            if not ok:
                failures += 1
            print(
                f"  {'PASS' if ok else 'FAIL'} {requirement_id}: "
                f"expected={expected_status} actual={actual_status}"
            )

        expected_stats = scenario["expected"]["stats"]
        print(
            "  stats: "
            f"expected={expected_stats} actual={result.stats}"
        )
        for key, value in expected_stats.items():
            if result.stats.get(key) != value:
                failures += 1
                print(f"  FAIL stats.{key}: expected={value} actual={result.stats.get(key)}")

    print(f"\n{'OK' if failures == 0 else 'FAILED'}: {failures} mismatch(es)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
