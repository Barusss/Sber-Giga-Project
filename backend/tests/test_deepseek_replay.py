"""Deterministic no-network checks for replaying exact private request bodies."""
import json

from scripts.replay_deepseek_call import JSON_SUFFIX, prepare_request, replay


def original():
    return {
        "model": "deepseek-flash",
        "thinking": {"type": "disabled"},
        "max_tokens": 3000,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Compare" + JSON_SUFFIX},
            {"role": "user", "content": json.dumps({
                "phase": "targeted", "tu_requirement": {"id": "tu-1"},
                "znp_candidates": [
                    {"atom_id": f"z-{i}", "text": f"Document item {i}"}
                    for i in range(8)
                ],
            })},
        ],
    }


def test_prepare_replay_preserves_system_prompt_without_duplicate_json_suffix():
    spec = prepare_request(original())
    assert spec["system_prompt"] == "Compare"
    assert spec["json_mode"] is True
    assert spec["max_tokens"] == 3000
    assert len(json.loads(spec["user_prompt"])["znp_candidates"]) == 8


def test_replay_is_not_billable_without_execute():
    class Never:
        def complete_json(self, **kwargs):
            raise AssertionError("Unexpected paid API request")

    result = replay(original(), max_tokens=6000, execute=False, client=Never())
    assert result["executed"] is False
    assert result["max_tokens"] == 6000
    assert result["user_chars"] > 0


def test_targeted_candidate_trim_is_explicit_and_reproducible():
    spec = prepare_request(original(), trim_candidates=3)
    assert len(json.loads(spec["user_prompt"])["znp_candidates"]) == 3
    assert spec["model"] == "deepseek-flash"


def test_execute_with_mock_passes_exact_prompt_and_config_independently():
    calls = []

    class Echo:
        def complete_json(self, **kwargs):
            calls.append(kwargs)
            return {"links": []}

    result = replay(original(), execute=True, client=Echo())
    assert result["executed"] is True
    assert calls == [{
        "system_prompt": "Compare",
        "user_prompt": original()["messages"][1]["content"],
    }]
