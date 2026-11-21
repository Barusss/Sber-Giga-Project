"""Offline, conservative checks for optional quote-ID replies (no provider calls)."""
from __future__ import annotations

import json

import httpx
import pytest

from app.ai.deepseek import DeepSeekClient, DeepSeekResponseError
from app.ai.embeddings import EmbeddingService
from app.core.config import Settings, settings
from app.requirements.evidence_refs import quote_choices, restore_link_quotes, link_schema
from app.requirements.schemas import TURequirement, ZNPClause, RequirementCategory
from app.requirements.znp_first import ZnPFirstAgent


def tu(text="Предусмотреть кабель 10 кВ для КЛ-1."):
    return TURequirement(
        id="t1", clause_number="1", category=RequirementCategory.other,
        text=text, fragment_id="t1", fragment_text=text,
    )


def znp(text="Для КЛ-1 предусмотреть кабель 10 кВ."):
    return ZNPClause(
        id="z1", clause_number="1", text=text, fragment_id="z1",
        fragment_text=text, sources=[{"fragment_id": "z1", "page": 1, "text": text}],
    )


def test_quote_choices_are_verbatim_and_deterministic():
    source = znp()
    first, lookup = quote_choices(source, "z", "КЛ-1 кабель 10 кВ")
    second, second_lookup = quote_choices(source, "z", "КЛ-1 кабель 10 кВ")
    assert first == second
    assert lookup == second_lookup
    assert first
    assert all(value in source.sources[0]["text"] for value in lookup.values())


def test_unknown_ids_cannot_become_quotes():
    result = restore_link_quotes(
        {"links": [{
            "requirement_id": "t1", "relation": "supports",
            "tu_evidence_id": "WRONG", "znp_evidence_id": "z0",
            "tu_quote": "model invented", "znp_quote": "model invented",
            "confidence": 0.99,
        }]},
        tu_maps={"t1": {"t0": "Предусмотреть кабель 10 кВ для КЛ-1."}},
        znp_maps={"_current": {"z0": "Для КЛ-1 предусмотреть кабель 10 кВ."}},
        mode="matching",
    )
    assert "tu_quote" not in result["links"][0]
    assert result["links"][0]["znp_quote"].startswith("Для КЛ-1")
    assert ZnPFirstAgent._validated_link(znp(), tu(), result["links"][0], 0.9) is None


def test_too_many_source_spans_fall_back_without_discarding_evidence():
    value = " ".join(f"Кабель {i} кВ для КЛ-{i}." for i in range(250))
    opts, lookup = quote_choices(znp(value), "z", value, limit=2)
    assert opts == [] and lookup == {}


def test_opt_in_matching_restores_quotes_and_keeps_python_validation(monkeypatch):
    monkeypatch.setattr(settings, "requirements_evidence_id_mode", True)

    class Fake:
        def complete_json(self, *, system_prompt, user_prompt):
            payload = json.loads(user_prompt)
            assert payload["evidence_format"] == "id_v1"
            assert "source_fragments" not in payload["znp_atom"]
            candidate = payload["tu_candidates"][0]
            assert "source_fragments" not in candidate
            return {"links": [{
                "requirement_id": "t1", "relation": "supports",
                "tu_evidence_id": candidate["quote_choices"][0]["id"],
                "znp_evidence_id": payload["znp_atom"]["quote_choices"][0]["id"],
                "confidence": 0.95, "reasoning": "Те же параметры",
            }], "search_more": False}

    agent = ZnPFirstAgent(ai_client=Fake(), embedder=EmbeddingService(enabled=False))
    requirement, atom = tu(), znp()
    agent._requirements = {"t1": requirement}
    reply = agent._judge(atom, [{
        "requirement_id": "t1", "clause_number": "1",
        "text": requirement.text, "similarity_score": 0.9,
    }], [])
    validated = agent._validated_link(atom, requirement, reply["links"][0], 0.9)
    assert validated is not None
    assert validated["tu_quote"] in requirement.text
    assert validated["quote"] in atom.sources[0]["text"]
    assert validated["relation"] == "supports"


def test_opt_in_targeted_uses_candidate_specific_quote_ids(monkeypatch):
    monkeypatch.setattr(settings, "requirements_evidence_id_mode", True)

    class Fake:
        def complete_json(self, *, system_prompt, user_prompt):
            payload = json.loads(user_prompt)
            assert payload["evidence_format"] == "id_v1"
            target = payload["znp_candidates"][0]
            return {"links": [{
                "atom_id": target["atom_id"], "relation": "supports",
                "tu_evidence_id": payload["tu_requirement"]["quote_choices"][0]["id"],
                "znp_evidence_id": target["quote_choices"][0]["id"],
                "confidence": 0.9, "reasoning": "Подтверждено",
            }]}

    agent = ZnPFirstAgent(ai_client=Fake(), embedder=EmbeddingService(enabled=False))
    requirement, atom = tu(), znp()
    reply = agent._judge_targeted(
        requirement, [{"clause_id": atom.id}], {atom.id: atom},
    )
    assert reply["links"][0]["tu_quote"] in requirement.text
    assert reply["links"][0]["znp_quote"] in atom.text
    assert agent._validated_link(atom, requirement, reply["links"][0], 0.9)


def test_opt_in_unsupported_source_falls_back_to_original_prompt(monkeypatch):
    monkeypatch.setattr(settings, "requirements_evidence_id_mode", True)
    text = " ".join(f"Кабель {i} кВ для КЛ-{i}." for i in range(250))

    class Fake:
        def complete_json(self, *, system_prompt, user_prompt):
            payload = json.loads(user_prompt)
            assert "evidence_format" not in payload
            assert "tu_candidates" in payload
            return {"links": [], "search_more": False}

    agent = ZnPFirstAgent(ai_client=Fake(), embedder=EmbeddingService(enabled=False))
    requirement, atom = tu(text), znp()
    agent._requirements = {"t1": requirement}
    assert agent._judge(atom, [{
        "requirement_id": "t1", "clause_number": "1",
        "text": requirement.text, "similarity_score": 0.9,
    }], [])["links"] == []


def test_strict_tool_schema_is_disabled_by_default():
    cfg = Settings(_env_file=None, deepseek_api_key="fake")
    assert not cfg.requirements_evidence_id_mode
    assert not cfg.deepseek_strict_evidence_tools
    schema = link_schema(targeted=False)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["links"]["items"]["additionalProperties"] is False


def test_strict_beta_tool_call_mock_transport_only():
    def handler(request):
        assert str(request.url) == "https://api.deepseek.com/beta/chat/completions"
        payload = json.loads(request.content)
        assert "response_format" not in payload
        assert payload["tools"][0]["function"]["strict"] is True
        assert payload["tool_choice"]["function"]["name"] == "submit_evidence_links"
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [{"function": {
                        "name": "submit_evidence_links",
                        "arguments": '{"links":[],"search_more":false}',
                    }}],
                },
            }],
        })
    cfg = Settings(
        _env_file=None, deepseek_api_key="fake", requirements_evidence_id_mode=True,
        deepseek_strict_evidence_tools=True,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = DeepSeekClient(config=cfg, http_client=http)
        reply = client.complete_json(
            system_prompt="Результат только JSON",
            user_prompt=json.dumps({
                "evidence_format": "id_v1", "phase": "primary",
                "znp_atom": {"id": "z1"}, "tu_candidates": [],
            }),
        )
    assert reply == {"links": [], "search_more": False}


def test_strict_mode_missing_forced_call_is_rejected_without_auto_retry():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {
                "content": '{"links":[]}', "tool_calls": [],
            }}],
        })

    cfg = Settings(
        _env_file=None, deepseek_api_key="fake", requirements_evidence_id_mode=True,
        deepseek_strict_evidence_tools=True,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DeepSeekResponseError, match="required structured"):
            DeepSeekClient(config=cfg, http_client=http).complete_json(
                system_prompt="test",
                user_prompt=json.dumps({
                    "evidence_format": "id_v1", "phase": "primary",
                    "znp_atom": {"id": "z1"},
                }),
            )
    assert len(calls) == 1
