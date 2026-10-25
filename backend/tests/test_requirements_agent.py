"""Embedding and agent-boundary tests for TU/ZnP matching."""
import json
import math
from collections import deque

import httpx

from app.ai.deepseek import DeepSeekClient
from app.ai.embeddings import EmbeddingService
from app.core.config import Settings
from app.requirements.agent import RequirementsAgent
from app.requirements.schemas import RequirementCategory, TURequirement, ZNPClause
from app.requirements.tools import (
    tool_check_list_coverage,
    tool_pick_best_evidence,
    tool_search_znp_clauses,
    tool_validate_quotes,
    tool_verify_engineering_params,
)

DIM = 384


def _axis(index: int) -> list[float]:
    vector = [0.0] * DIM
    vector[index] = 1.0
    return vector


class FakeProvider:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, texts):
        self.calls.append(list(texts))
        return [_axis(0) for _ in texts]


def test_embedding_service_prefixes_and_caches():
    provider = FakeProvider()
    service = EmbeddingService(
        provider=provider,
        model="fake-e5-384",
    )
    documents = service.embed_documents(["мощность 150 кВт", "счетчик"])
    query = service.embed_query("какая мощность?")

    assert len(documents) == 2
    assert len(documents[0]) == DIM
    assert query == _axis(0)
    assert provider.calls[0] == [
        "passage: мощность 150 кВт",
        "passage: счетчик",
    ]
    assert provider.calls[1] == ["query: какая мощность?"]
    assert service.status()["enabled"] is True
    assert service.status()["reason"] is None


def test_embedding_service_degrades_on_wrong_dimension():
    service = EmbeddingService(
        provider=lambda texts: [[0.0, 1.0, 2.0] for _ in texts],
        model="fake-e5-384",
    )
    assert service.embed_documents(["текст"]) == []
    assert service.enabled is False
    assert "384" in service.reason
    assert service.embed_query("другой") is None


def test_embedding_service_disabled_without_package():
    service = EmbeddingService(enabled=False)
    assert service.embed_documents(["текст"]) == []
    assert service.embed_query("текст") is None
    assert service.status()["reason"] == "EMBEDDING_ENABLED=false"


def test_hybrid_search_adds_semantic_channel_and_numbers_bonus():
    clauses = [
        ZNPClause(id="a", clause_number="1", text="совершенно посторонний текст"),
        ZNPClause(id="b", clause_number="2", text="мощность 150 кВт"),
    ]
    results = tool_search_znp_clauses(
        "присоединяемая мощность",
        clauses,
        top_k=2,
        query_embedding=_axis(0),
        clause_embeddings=[_axis(0), _axis(1)],
    )
    assert [item["clause_id"] for item in results] == ["a", "b"]
    assert results[0]["channels"] == ["lexical", "semantic"]
    assert results[0]["semantic_score"] == 1.0
    assert results[1]["semantic_score"] == 0.0

    lexical = tool_search_znp_clauses("150 кВт", clauses, top_k=2)
    assert lexical[0]["clause_id"] == "b"
    assert lexical[0]["channels"] == ["lexical"]
    assert lexical[0]["semantic_score"] is None


def test_search_does_not_match_only_by_leading_clause_number():
    clauses = [
        ZNPClause(
            id="znp-1",
            clause_number="4.2",
            text="4.2. Выполнить заземление электроустановки.",
        ),
    ]

    result = tool_search_znp_clauses(
        "4.2. Выполнить огнезащиту кабельных линий в электротехнических помещениях.",
        clauses,
        top_k=1,
    )

    assert result[0]["lexical_score"] < 0.25
    assert result[0]["similarity_score"] < 0.25


def test_search_ignores_leading_clause_number_in_paraphrase():
    clauses = [
        ZNPClause(
            id="znp-1",
            clause_number="2.1",
            text="2.1. Место подключения определить по меже, выделенной застройщику.",
        ),
    ]

    result = tool_search_znp_clauses(
        "2.1. Точку присоединения разместить на границе земельного участка заявителя.",
        clauses,
        top_k=1,
    )

    assert result[0]["lexical_score"] == 0.0


def test_engineering_params_validator_reports_missing_znp_values():
    result = tool_verify_engineering_params(
        "Максимальная мощность 150 кВт, напряжение 10 кВ, "
        "II категория надёжности, класс точности 0.5S.",
        "Максимальная мощность 100 кВт.",
    )
    assert result["params_match"] is False
    assert len(result["mismatches"]) == 4
    messages = " ".join(result["mismatches"])
    assert "мощности" in messages
    assert "напряжения" in messages
    assert "категория надёжности" in messages
    assert "класс точности" in messages


def _semantic_vector(score: float) -> list[float]:
    return [score, math.sqrt(max(0.0, 1.0 - score * score))] + [0.0] * (DIM - 2)


def _agent_with_replies(replies, embedder=None):
    queue = deque(replies)

    def handler(request):
        reply = queue.popleft() if queue else replies[-1]
        content = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": content}, "finish_reason": "stop"}
                ]
            },
        )

    config = Settings(
        deepseek_api_key="test-key",
        deepseek_base_url="https://deepseek.invalid",
        embedding_enabled=False,
    )
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = DeepSeekClient(config=config, http_client=http_client)
    return RequirementsAgent(
        ai_client=client,
        max_steps=3,
        embedder=embedder or EmbeddingService(enabled=False),
    )


def _tu() -> TURequirement:
    return TURequirement(
        id="tu-1",
        clause_number="1.1",
        category=RequirementCategory.power,
        text="Максимальная мощность 150 кВт.",
    )


def _znp() -> ZNPClause:
    return ZNPClause(
        id="znp-1",
        clause_number="3.1",
        text="Предусмотреть максимальную мощность 150 кВт.",
    )


def test_deterministic_fallback_does_not_fulfill_unrelated_semantic_candidate():
    tu = TURequirement(
        id="demo-tu-3",
        clause_number="п. 4.1",
        category=RequirementCategory.metering,
        text=(
            "На границе балансовой принадлежности установить трёхфазный "
            "прибор учёта электрической энергии класса точности не ниже 0.5S."
        ),
    )
    znp = [
        ZNPClause(
            id="demo-znp-1",
            clause_number="Раздел 2.1",
            text=(
                "Присоединение проектируемой трансформаторной подстанции "
                "выполнить от сети 10 кВ."
            ),
        ),
        ZNPClause(
            id="demo-znp-2",
            clause_number="Раздел 3.2",
            text=(
                "Расчётная максимальная мощность энергопринимающих устройств "
                "по заданию составляет 100 кВт по II категории надёжности."
            ),
        ),
    ]

    def provider(texts):
        vectors = []
        for text in texts:
            if text == znp[1].text:
                vectors.append(_semantic_vector(0.583))
            elif text == znp[0].text:
                vectors.append(_semantic_vector(0.364))
            else:
                vectors.append(_semantic_vector(1.0))
        return vectors

    agent = _agent_with_replies(
        ["это не JSON"],
        embedder=EmbeddingService(provider=provider, model="test-semantic"),
    )
    response = agent.verify_all([tu], znp)
    item = response.checked_requirements[0]

    assert item.method == "deterministic"
    assert item.status.value == "needs_review"
    assert item.matched_clause is not None
    assert item.matched_clause.id == "demo-znp-2"
    assert "класс точности" in item.reasoning
    steps = response.trace_info["agent_runs"][0]["steps"]
    param_step = next(step for step in steps if step["tool"] == "verify_params")
    assert param_step["output"]["params_match"] is False


def test_deterministic_fallback_requires_lexical_support_for_fulfilled():
    tu = TURequirement(
        id="tu-no-params",
        clause_number="1.1",
        category=RequirementCategory.other,
        text="Обеспечить выполнение организационных мероприятий.",
    )
    znp = [
        ZNPClause(
            id="znp-unrelated",
            clause_number="2.1",
            text="Совершенно другой текст про трансформатор.",
        )
    ]
    embedder = EmbeddingService(
        provider=lambda texts: [_axis(0) for _ in texts],
        model="test-semantic",
    )
    agent = _agent_with_replies(["это не JSON"], embedder=embedder)
    response = agent.verify_all([tu], znp)
    item = response.checked_requirements[0]

    assert item.method == "deterministic"
    assert item.status.value == "needs_review"
    assert "лексической опоры" in item.reasoning


def test_agent_llm_path_reports_method_and_channels():
    agent = _agent_with_replies([
        {
            "action": "call_tool",
            "thought": "поиск",
            "tool_name": "search_znp",
            "tool_args": {"query": "максимальная мощность 150 кВт"},
        },
        {
            "action": "finish",
            "status": "fulfilled",
            "confidence_score": 0.91,
            "matched_znp_clause_id": "znp-1",
            "tu_quote": "Максимальная мощность 150 кВт",
            "znp_quote": "максимальную мощность 150 кВт",
            "reasoning": "Параметры совпадают.",
        },
    ])
    response = agent.verify_all([_tu()], [_znp()])
    item = response.checked_requirements[0]
    assert item.status.value == "fulfilled"
    assert item.method == "llm"
    assert item.channels == ["lexical"]
    assert response.trace_info["agent_runs"][0]["steps"][0]["channels"] == ["lexical"]


def test_agent_llm_fulfilled_is_downgraded_when_znp_lacks_parameter():
    tu = TURequirement(
        id="tu-metering",
        clause_number="4.1",
        category=RequirementCategory.metering,
        text="Установить прибор учёта класса точности 0.5S.",
    )
    znp = ZNPClause(
        id="znp-power",
        clause_number="3.2",
        text="Максимальная мощность энергопринимающих устройств 100 кВт.",
    )
    agent = _agent_with_replies([
        {
            "action": "finish",
            "status": "fulfilled",
            "confidence_score": 0.95,
            "matched_znp_clause_id": "znp-power",
            "tu_quote": "класса точности 0.5S",
            "znp_quote": "мощность энергопринимающих устройств 100 кВт",
            "reasoning": "Модель ошибочно сочла требование выполненным.",
        },
    ])
    response = agent.verify_all([tu], [znp])
    item = response.checked_requirements[0]

    assert item.status.value == "needs_review"
    assert "Валидатор параметров" in item.reasoning
    assert "класс точности" in item.reasoning


def test_agent_invalid_json_falls_back_without_crashing():
    agent = _agent_with_replies(["это не JSON"])
    response = agent.verify_all([_tu()], [_znp()])
    item = response.checked_requirements[0]
    assert item.method == "deterministic"
    assert item.channels == ["lexical"]
    assert item.status.value in {"fulfilled", "needs_review", "missing"}


def test_agent_invalid_tool_args_falls_back_without_crashing():
    agent = _agent_with_replies([
        {
            "action": "call_tool",
            "tool_name": "search_znp",
            "tool_args": "not-a-dict",
        }
    ])
    response = agent.verify_all([_tu()], [_znp()])
    item = response.checked_requirements[0]
    assert item.method == "deterministic"
    assert item.tu_quote == _tu().text


def test_agent_replaces_invented_quote_with_source_text():
    agent = _agent_with_replies([
        {
            "action": "finish",
            "status": "fulfilled",
            "confidence_score": 0.91,
            "matched_znp_clause_id": "znp-1",
            "tu_quote": "выдуманная цитата",
            "znp_quote": "тоже выдумка",
            "reasoning": "Параметры совпадают.",
        },
    ])
    response = agent.verify_all([_tu()], [_znp()])
    item = response.checked_requirements[0]
    assert "выдуманная" not in item.tu_quote
    assert item.tu_quote == _tu().text[:120]
    assert "выдумка" not in item.znp_quote
    assert item.status.value == "needs_review"
    finish_step = next(
        step for step in response.trace_info["agent_runs"][0]["steps"]
        if step["tool"] == "finish_and_validate"
    )
    assert finish_step["evidence_check"]["quote_hallucinated"] is True
    assert finish_step["evidence_check"]["quote_replaced"] is True


def test_agent_uses_embedding_channel_and_reports_embedding_trace():
    embedder = EmbeddingService(
        provider=lambda texts: [_axis(0) for _ in texts],
        model="fake-e5-384",
    )
    agent = _agent_with_replies(["это не JSON"], embedder=embedder)
    response = agent.verify_all([_tu()], [_znp()])
    item = response.checked_requirements[0]
    assert item.channels == ["lexical", "semantic"]
    assert response.trace_info["embedding"]["enabled"] is True


LAW_LIST_TU = (
    "1.3.1. Предусмотреть выполнение требований:\n"
    "- ГОСТ Р 12345-2020 по проектированию;\n"
    "- СП 31.13330.2021 по проектированию;\n"
    "- Федеральный закон № 123-ФЗ по проектированию."
)


def test_list_coverage_requires_every_law():
    partial = tool_check_list_coverage(
        LAW_LIST_TU,
        "Предусмотреть выполнение требований ГОСТ Р 12345-2020 по проектированию.",
    )
    assert partial["is_list"] is True
    assert partial["items_total"] == 3
    assert partial["items_covered"] == 1
    assert partial["covered"] is False
    assert partial["coverage_ratio"] < 1
    assert partial["uncovered_items"]
    assert all(len(item) <= 160 for item in partial["uncovered_items"])

    complete = tool_check_list_coverage(
        LAW_LIST_TU,
        "Предусмотреть ГОСТ Р 12345-2020, СП 31.13330.2021 "
        "и Федеральный закон № 123-ФЗ.",
    )
    assert complete["is_list"] is True
    assert complete["items_covered"] == complete["items_total"] == 3
    assert complete["covered"] is True
    assert complete["uncovered_items"] == []


def test_list_coverage_detects_inline_numbered_items():
    tu_text = (
        "Выполнить: 1) разработать проектную документацию; "
        "2) получить разрешение; 3) выполнить монтаж."
    )
    result = tool_check_list_coverage(tu_text, "Разработать проектную документацию.")
    assert result["is_list"] is True
    assert result["items_total"] == 3
    assert result["covered"] is False


def test_list_coverage_does_not_treat_ordinary_paragraph_as_list():
    tu_text = (
        "Присоединяемая мощность 726,9 кВт при номинальном напряжении "
        "0,4 кВ; точку присоединения разместить на границе участка."
    )
    result = tool_check_list_coverage(
        tu_text, "Мощность 726,9 кВт, напряжение 0,4 кВ."
    )
    assert result["is_list"] is False
    assert result["covered"] is True
    assert result["uncovered_items"] == []


def test_pick_best_evidence_returns_short_exact_substring_from_glued_list():
    lines = []
    for number in range(99, 126):
        tail = (
            "предусмотреть выполнение работ по ГОСТ Р 12345-2020."
            if number == 115
            else "выполнить работы по проектированию."
        )
        lines.append(f"{number}. Мероприятие {number}: {tail}")
    candidate = "\n".join(lines)

    result = tool_pick_best_evidence(
        "выполнить работы по ГОСТ Р 12345-2020", candidate
    )

    assert result["quote"] in candidate
    assert result["quote"] != candidate
    assert 20 <= len(result["quote"]) <= 600
    assert result["is_atomic"] is True
    assert "115" in result["quote"]


def test_deterministic_fallback_marks_partially_covered_list_for_review():
    tu = TURequirement(
        id="tu-list",
        clause_number="1.3.1",
        category=RequirementCategory.other,
        text=LAW_LIST_TU,
    )
    znp = [
        ZNPClause(
            id="znp-list",
            clause_number="1.3.1",
            text=(
                "1.3.1. Предусмотреть выполнение требований "
                "ГОСТ Р 12345-2020 по проектированию."
            ),
        )
    ]
    agent = _agent_with_replies(["это не JSON"])
    response = agent.verify_all([tu], znp)
    item = response.checked_requirements[0]

    assert item.status.value == "needs_review"
    assert "Перечень подтверждён не полностью" in item.reasoning
    assert "1 из 3" in item.reasoning
    steps = response.trace_info["agent_runs"][0]["steps"]
    coverage_step = next(step for step in steps if step["tool"] == "check_list_coverage")
    assert coverage_step["output"]["is_list"] is True
    assert coverage_step["output"]["covered"] is False
    assert any(step["tool"] == "pick_evidence" for step in steps)


def test_deterministic_fallback_quote_is_short_substring_of_glued_list():
    lines = []
    for number in range(99, 126):
        tail = (
            "предусмотреть выполнение мероприятий по пункту 115."
            if number == 115
            else "выполнить работы по проектированию."
        )
        lines.append(f"{number}. Мероприятие {number}: {tail}")
    candidate = "\n".join(lines)
    tu = TURequirement(
        id="tu-glued",
        clause_number="2.5",
        category=RequirementCategory.other,
        text="2.5. Предусмотреть выполнение мероприятий по пункту 115.",
    )
    znp = [ZNPClause(id="znp-glued", clause_number="99-125", text=candidate)]

    agent = _agent_with_replies(["это не JSON"])
    response = agent.verify_all([tu], znp)
    item = response.checked_requirements[0]

    assert item.znp_quote
    assert item.znp_quote in candidate
    assert item.znp_quote != candidate
    assert len(item.znp_quote) < len(candidate)
    assert len(item.znp_quote) <= 600
    steps = response.trace_info["agent_runs"][0]["steps"]
    assert any(step["tool"] == "pick_evidence" for step in steps)


def test_agent_llm_fulfilled_is_downgraded_when_list_is_partially_covered():
    tu = TURequirement(
        id="tu-list-llm",
        clause_number="1.3.1",
        category=RequirementCategory.other,
        text=LAW_LIST_TU,
    )
    znp = ZNPClause(
        id="znp-list-llm",
        clause_number="1.3.1",
        text=(
            "1.3.1. Предусмотреть выполнение требований "
            "ГОСТ Р 12345-2020 по проектированию."
        ),
    )
    agent = _agent_with_replies([
        {
            "action": "finish",
            "status": "fulfilled",
            "confidence_score": 0.97,
            "matched_znp_clause_id": "znp-list-llm",
            "tu_quote": "ГОСТ Р 12345-2020",
            "znp_quote": "выполнение требований ГОСТ Р 12345-2020",
            "reasoning": "Модель считает требование выполненным.",
        },
    ])
    response = agent.verify_all([tu], [znp])
    item = response.checked_requirements[0]

    assert item.status.value == "needs_review"
    assert item.confidence_score <= 0.65
    assert "Перечень подтверждён не полностью" in item.reasoning
    assert "1 из 3" in item.reasoning
    finish_step = next(
        step for step in response.trace_info["agent_runs"][0]["steps"]
        if step["tool"] == "finish_and_validate"
    )
    assert finish_step["coverage_check"]["is_list"] is True
    assert finish_step["coverage_check"]["covered"] is False
    assert finish_step["evidence_check"]["final_quote_atomic"] is True


def test_validate_quotes_normalizes_typography_and_reports_atomicity():
    tu_full = "Требуется мощность — 150 кВт."
    znp_full = "Предусмотреть «мощность — 150 кВт»… не ниже."
    result = tool_validate_quotes(
        tu_full,
        "мощность - 150 кВт",
        znp_full,
        'Предусмотреть "мощность - 150 кВт"... не ниже.',
    )

    assert result["tu_quote_valid"] is True
    assert result["znp_quote_valid"] is True
    assert result["znp_quote_present"] is True
    assert result["znp_quote_atomic"] is True
    assert result["tu_quote_chars"] > 0
    assert result["znp_quote_chars"] > 0

    empty = tool_validate_quotes(tu_full, "мощность - 150 кВт", znp_full, "  ")
    assert empty["znp_quote_present"] is False
    assert empty["znp_quote_atomic"] is False
    assert empty["is_valid"] is False

    glued = "\n".join(f"{number}. Пункт {number}." for number in range(99, 103))
    long_quote = tool_validate_quotes(tu_full, "мощность - 150 кВт", glued, glued)
    assert long_quote["znp_quote_valid"] is True
    assert long_quote["znp_quote_atomic"] is False

