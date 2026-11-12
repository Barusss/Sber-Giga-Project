import json
from types import SimpleNamespace

import pytest

from app.ai.embeddings import EmbeddingService
from app.core.models import Fragment
from app.requirements.atomization import atomize_fragments
from app.requirements.retrieval import RequirementRetriever
from app.requirements.schemas import RequirementCategory, TURequirement


def req(index, text):
    return TURequirement(id=str(index), clause_number=str(index), text=text,
                         category=RequirementCategory.other, fragment_id=f"f{index}", page=index)


def atom(text):
    return SimpleNamespace(id="z1", text=text, sources=("source",), warnings=())


def test_every_atom_gets_search_and_candidates_can_be_expanded():
    requirements = [req(i, f"Кабель {i} кВ") for i in range(1, 9)]
    search = RequirementRetriever(requirements, EmbeddingService(enabled=False), batch_size=3)
    first = search.search(atom("Кабель 5 кВ"))
    second = search.search(atom("Кабель 5 кВ"), offset=3)
    assert len(first["candidates"]) == 3
    assert len(second["candidates"]) == 3
    assert not ({x["requirement_id"] for x in first["candidates"]} &
                {x["requirement_id"] for x in second["candidates"]})
    assert first["has_more"] is True
    assert first["atom_id"] == "z1"
    assert first["degraded"] is True
    assert all(x["fragment_id"] for x in first["candidates"])


def test_rephrased_query_uses_its_own_embedding():
    calls = []
    def provider(texts):
        calls.extend(texts)
        return [[1.0] + [0.0] * 383 for _ in texts]
    embedder = EmbeddingService(provider=provider, model="example")
    search = RequirementRetriever([req(1, "кабель")], embedder)
    search.search(atom("первый запрос"))
    search.search(atom("первый запрос"), query="другой запрос")
    assert "первый запрос" in calls
    assert "другой запрос" in calls


def test_neighbor_context_retains_identifiers_and_deduplicates():
    search = RequirementRetriever([req(i, f"Требование {i}") for i in range(1, 7)],
                                  EmbeddingService(enabled=False), neighbor_window=2)
    context = search.context(["3", "4"], known_ids={"2"})
    assert [x["requirement_id"] for x in context] == ["1", "3", "4", "5", "6"]
    assert context[0]["fragment_id"] == "f1"


def test_invalid_limits_are_rejected():
    with pytest.raises(ValueError):
        RequirementRetriever([], EmbeddingService(enabled=False), batch_size=0)
    with pytest.raises(ValueError):
        RequirementRetriever([], EmbeddingService(enabled=False), neighbor_window=-1)


def test_all_100_candidates_can_be_paged_without_implying_coverage():
    search = RequirementRetriever([req(i, f"Кабель {i} кВ") for i in range(1, 101)],
                                  EmbeddingService(enabled=False), batch_size=10)
    pages = [search.search(atom("Кабель"), offset=i) for i in range(0, 100, 10)]
    assert len({c["requirement_id"] for page in pages for c in page["candidates"]}) == 100
    assert pages[-1]["has_more"] is False
    assert pages[-1]["retrieval_complete"] is False


def test_embedding_failure_falls_back_and_reports_reason():
    def broken_provider(texts):
        raise RuntimeError("provider unavailable")
    search = RequirementRetriever([req(1, "кабель")], EmbeddingService(provider=broken_provider))
    result = search.search(atom("кабель"))
    assert result["candidates"][0]["channels"] == ["lexical"]
    assert result["degraded"] is True
    assert "provider unavailable" in result["embedding_status"]["reason"]


def test_section_expansion_and_known_items_are_not_repeated():
    items = [SimpleNamespace(id=str(i), clause_number=str(i), text=f"Кабель {i}",
                             fragment_id=f"f{i}", page=i, section_path="section")
             for i in range(1, 8)]
    search = RequirementRetriever(items, EmbeddingService(enabled=False), neighbor_window=0)
    context = search.context(["3", "4"], known_ids={"1", "2"}, include_section=True)
    assert [x["requirement_id"] for x in context] == ["3", "4", "5", "6", "7"]


def test_large_section_is_not_added_wholesale():
    items = [SimpleNamespace(id=str(i), clause_number=str(i), text=f"Кабель {i}",
                             fragment_id=f"f{i}", page=i, section_path="section")
             for i in range(1, 8)]
    search = RequirementRetriever(items, EmbeddingService(enabled=False), neighbor_window=1, max_section_size=3)
    assert [x["requirement_id"] for x in search.context(["3"], include_section=True)] == ["2", "3", "4"]


def test_empty_candidates_report_no_match_without_a_verdict():
    search = RequirementRetriever([], EmbeddingService(enabled=False))
    result = search.search(atom("кабель"))
    assert result["candidates"] == []
    assert result["has_more"] is False
    assert result["retrieval_complete"] is False
    assert result["degradation_reason"] == "EMBEDDING_ENABLED=false"


def test_real_atom_and_requirement_sources_serialize_with_context():
    heading = Fragment(id="h", document_id="d", page=1, position=0,
                       text="1. Общие положения", block_type="heading", section_path="1. Общие положения")
    body = Fragment(id="b", document_id="d", page=1, position=1,
                    text="1.1. Кабель.", block_type="paragraph", section_path=heading.text)
    a = atomize_fragments("d", [heading, body])[0]
    search = RequirementRetriever([a], EmbeddingService(enabled=False), neighbor_window=0)
    result = search.search(a)
    assert result["atom_context_sources"][0]["fragment_id"] == "h"
    assert result["candidates"][0]["context_sources"][0]["fragment_id"] == "h"
    assert search.context([a.id])[0]["context_sources"][0]["fragment_id"] == "h"
    json.dumps(result)
    json.dumps(search.context([a.id]))


def test_pagination_uses_original_ranking_after_embedding_channel_fails():
    calls = []
    def provider(texts):
        calls.append(texts)
        return [[1.0] + [0.0] * 383 for _ in texts]
    embedder = EmbeddingService(provider=provider)
    search = RequirementRetriever([req(i, "кабель") for i in range(1, 6)], embedder, batch_size=2)
    first = search.search(atom("кабель"))
    embedder.enabled = False
    second = search.search(atom("кабель"), offset=2)
    assert not ({c["requirement_id"] for c in first["candidates"]} &
                {c["requirement_id"] for c in second["candidates"]})
    assert second["ranking_channels"] == first["ranking_channels"]


def test_large_section_truncation_is_explicit():
    items = [SimpleNamespace(id=str(i), clause_number=str(i), text=f"Кабель {i}",
                             fragment_id=f"f{i}", page=i, section_path="section") for i in range(1, 8)]
    search = RequirementRetriever(items, EmbeddingService(enabled=False), neighbor_window=0, max_section_size=3)
    result = search.context_details(["3"], include_section=True)
    assert result["sections_skipped"] == [{"section_path": "section", "size": 7, "limit": 3}]
