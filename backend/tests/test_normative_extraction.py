"""DeepSeek responses are mocked: tests never use a real API key."""
import json
from io import BytesIO

import pytest
from app.ai.deepseek import DeepSeekNotConfigured
from app.documents.parser import ExtractedBlock


def upload(client, monkeypatch, blocks):
    monkeypatch.setattr("app.documents.router.extract_blocks", lambda data, suffix: blocks)
    project = client.post("/api/v1/projects", json={"name": "Normative check"}).json()
    response = client.post(
        f"/api/v1/projects/{project['id']}/documents", data={"kind": "other"},
        files={"file": ("gost.pdf", BytesIO(b"dummy pdf"), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    return project["id"], response.json()["id"]


def test_extract_multiple_pages_and_idempotency(client, monkeypatch):
    blocks = [
        ExtractedBlock(1, 0, "ГОСТ 32144—2013 Межгосударственный стандарт"),
        ExtractedBlock(5, 0, "Нормативные ссылки: ГОСТ 29322—92 Стандартные напряжения"),
        ExtractedBlock(6, 0, "Согласно ГОСТ 29322—92 нормы ... и по ГОСТ 30804.4.30—2013"),
        ExtractedBlock(19, 0, "Проектные нормы по СП 256.1325800.2016 также определяются"),
    ]
    _, doc_id = upload(client, monkeypatch, blocks)
    batches = []

    def fake(self, *, system_prompt, user_prompt):
        data = json.loads(user_prompt)
        batches.append(data)
        assert data["candidates"]
        return {"selected": [c["id"] for c in data["candidates"]
                             if c["code"] != "ГОСТ 32144-2013"]}

    monkeypatch.setattr("app.normatives.extraction.DeepSeekClient.complete_json", fake)
    url = f"/api/v1/documents/{doc_id}/extract-facts"
    res = client.post(url)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["deepseek_calls"] == 1
    assert data["created"] == 3
    values = {f["value"]["code"]: f for f in data["facts"]}
    assert set(values) == {"ГОСТ 29322-92", "ГОСТ 30804.4.30-2013", "СП 256.1325800.2016"}
    assert {e["page"] for e in values["ГОСТ 29322-92"]["evidence"]} == {5, 6}
    assert all(f["review_status"] == "pending" for f in data["facts"])
    assert all(f["extraction_version"] == "deepseek-normative-v2" for f in data["facts"])
    assert len(client.get(f"/api/v1/documents/{doc_id}/facts?fact_type=normative_reference").json()) == 3
    rerun = client.post(url)
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["created"] == 0
    assert rerun.json()["skipped_existing"] >= 4
    assert len(client.get(f"/api/v1/documents/{doc_id}/facts").json()) == 3


def test_untrusted_model_cannot_invent_reference_or_source(client, monkeypatch):
    _, doc_id = upload(client, monkeypatch, [
        ExtractedBlock(6, 0, "Требования по ГОСТ 29322—92 подтверждены текстом."),
    ])
    monkeypatch.setattr("app.normatives.extraction.DeepSeekClient.complete_json",
                        lambda self, **kwargs: {"selected": [999, True, "0"]})
    res = client.post(f"/api/v1/documents/{doc_id}/extract-facts")
    assert res.status_code == 200, res.text
    assert res.json()["created"] == 0
    assert res.json()["skipped_invalid"] == 3
    assert client.get(f"/api/v1/documents/{doc_id}/facts").json() == []


def test_malformed_llm_result_is_rejected_without_writing(client, monkeypatch):
    _, doc_id = upload(client, monkeypatch, [
        ExtractedBlock(5, 0, "ГОСТ 29322—92 Нормативные ссылки"),
    ])
    monkeypatch.setattr("app.normatives.extraction.DeepSeekClient.complete_json",
                        lambda self, **kwargs: {"selected": "invalid"})
    res = client.post(f"/api/v1/documents/{doc_id}/extract-facts")
    assert res.status_code == 502
    assert client.get(f"/api/v1/documents/{doc_id}/facts").json() == []


def test_missing_key_and_approved_project(client, monkeypatch):
    pid, doc_id = upload(client, monkeypatch, [
        ExtractedBlock(3, 0, "Согласно ГОСТ 29322—92 стандартному напряжению"),
    ])

    def unavailable(self, **kwargs):
        raise DeepSeekNotConfigured("private error")

    monkeypatch.setattr("app.normatives.extraction.DeepSeekClient.complete_json", unavailable)
    url = f"/api/v1/documents/{doc_id}/extract-facts"
    assert client.post(url).status_code == 503
    assert "private error" not in client.post(url).text
    assert client.get(f"/api/v1/documents/{doc_id}/facts").json() == []
    assert client.post(f"/api/v1/projects/{pid}/approve").status_code == 200
    assert client.post(url).status_code == 409
    assert client.post("/api/v1/documents/missing/extract-facts").status_code == 404


def test_no_candidates_does_not_call_model(client, monkeypatch):
    _, doc_id = upload(client, monkeypatch, [
        ExtractedBlock(2, 0, "Обычный технический текст без нормативных ссылок"),
    ])
    def fail(self, **kwargs):
        pytest.fail("Model must not be called")
    monkeypatch.setattr("app.normatives.extraction.DeepSeekClient.complete_json", fail)
    res = client.post(f"/api/v1/documents/{doc_id}/extract-facts")
    assert res.status_code == 200, res.text
    assert res.json()["deepseek_calls"] == 0
    assert res.json()["candidates"] == 0


def test_multiple_batches_reach_last_page(client, monkeypatch):
    blocks = [ExtractedBlock(i + 1, 0, f"Нормативная ссылка ГОСТ {20000 + i}-2020 используется в проекте.")
              for i in range(60)]
    _, doc_id = upload(client, monkeypatch, blocks)
    sizes = []

    def fake(self, *, system_prompt, user_prompt):
        data = json.loads(user_prompt)
        sizes.append(len(data["candidates"]))
        return {"selected": [candidate["id"] for candidate in data["candidates"]]}

    monkeypatch.setattr("app.normatives.extraction.DeepSeekClient.complete_json", fake)
    res = client.post(f"/api/v1/documents/{doc_id}/extract-facts")
    assert res.status_code == 200, res.text
    assert res.json()["created"] == 60
    assert res.json()["deepseek_calls"] == 3
    assert sizes == [25, 25, 10]
    pages = {e["page"] for f in res.json()["facts"] for e in f["evidence"]}
    assert pages == set(range(1, 61))


def test_normalization_sections_and_own_standard(client, monkeypatch):
    blocks = [
        ExtractedBlock(1, 0, "МЕЖГОСУДАРСТВЕННЫЙ СТАНДАРТ\nГОСТ\n32144 —\n2013\nЭлектрическая энергия"),
        ExtractedBlock(2, 0, "Предисловие\nГОСТ 1.0—92 устанавливает порядок разработки."),
        ExtractedBlock(3, 0, "Содержание\n2 Нормативные ссылки.....................4"),
        ExtractedBlock(4, 0, "1 Область применения\nОсновные положения."),
        ExtractedBlock(5, 0, "2 Нормативные ссылки\nГОСТ 29322—92 Стандартные напряжения\n"
                           "ГОСТ 30804.4.30—2013 определяет методы измерения."),
        ExtractedBlock(6, 0, "3 Термины, определения и обозначения\n"
                           "Согласно ГОСТ 29322 и по ГОСТ 30804.4.30 проводится проверка."),
        ExtractedBlock(19, 0, "Библиография\nIEC 61000-4-15:2010 Flickermeter requirements."),
    ]
    _, doc_id = upload(client, monkeypatch, blocks)

    def select_all(self, *, system_prompt, user_prompt):
        data = json.loads(user_prompt)
        assert all(item["code"] != "ГОСТ 32144-2013" for item in data["candidates"])
        return {"selected": [item["id"] for item in data["candidates"]]}

    monkeypatch.setattr("app.normatives.extraction.DeepSeekClient.complete_json", select_all)
    res = client.post(f"/api/v1/documents/{doc_id}/extract-facts")
    assert res.status_code == 200, res.text
    assert res.json()["skipped_self"] == 1
    assert res.json()["created"] == 4
    facts = {fact["value"]["code"]: fact for fact in res.json()["facts"]}
    assert set(facts) == {
        "ГОСТ 1.0-92", "ГОСТ 29322-92", "ГОСТ 30804.4.30-2013", "IEC 61000-4-15:2010"
    }
    assert facts["ГОСТ 1.0-92"]["value"]["reference_kind"] == "informative"
    assert facts["ГОСТ 1.0-92"]["value"]["contexts"] == ["foreword"]
    assert facts["ГОСТ 29322-92"]["value"]["reference_kind"] == "normative"
    assert facts["ГОСТ 29322-92"]["value"]["contexts"] == ["body", "normative_references"]
    assert len(facts["ГОСТ 29322-92"]["evidence"]) == 2
    assert facts["ГОСТ 29322-92"]["value"]["variants"] == ["ГОСТ 29322", "ГОСТ 29322-92"]
    assert facts["ГОСТ 30804.4.30-2013"]["value"]["variants"] == [
        "ГОСТ 30804.4.30", "ГОСТ 30804.4.30-2013"
    ]
    assert facts["IEC 61000-4-15:2010"]["value"]["reference_kind"] == "informative"
    assert facts["IEC 61000-4-15:2010"]["value"]["contexts"] == ["bibliography"]
    rerun = client.post(f"/api/v1/documents/{doc_id}/extract-facts")
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["created"] == 0
    assert rerun.json()["deepseek_calls"] == 0


def test_existing_v1_records_reconciled_without_llm(client, monkeypatch):
    blocks = [
        ExtractedBlock(1, 0, "МЕЖГОСУДАРСТВЕННЫЙ СТАНДАРТ\nГОСТ\n32144 —\n2013\nЭлектрическая энергия"),
        ExtractedBlock(2, 0, "Предисловие\nГОСТ 1.0—92 основные положения\nГОСТ 32144—2013 введён."),
        ExtractedBlock(5, 0, "2 Нормативные ссылки\nГОСТ 30804.4.30—2013 Методы измерений"),
        ExtractedBlock(6, 0, "3 Термины, определения и обозначения\n"
                           "Определено ГОСТ 30804.4.30 в требованиях"),
    ]
    _, doc_id = upload(client, monkeypatch, blocks)
    fragments = client.get(f"/api/v1/documents/{doc_id}/fragments").json()
    by_page = {fragment["page"]: fragment["id"] for fragment in fragments}

    def save(code, page, quote, version):
        result = client.post(f"/api/v1/documents/{doc_id}/facts", json={
            "fact_type": "normative_reference", "value": {"code": code},
            "extraction_version": version,
            "evidence": [{"fragment_id": by_page[page], "quote": quote}],
        })
        assert result.status_code == 201, result.text
        return result.json()["id"]

    old_self = save("ГОСТ 32144-2013", 2, "ГОСТ 32144—2013", "deepseek-normative-v1")
    older = save("ГОСТ 30804.4.30", 6, "ГОСТ 30804.4.30", "deepseek-normative-v1")
    newer = save("ГОСТ 30804.4.30-2013", 5, "ГОСТ 30804.4.30—2013", "deepseek-normative-v1")
    foreword = save("ГОСТ 1.0-92", 2, "ГОСТ 1.0—92", "deepseek-normative-v1")
    manual = save("ГОСТ 1.0-92", 2, "ГОСТ 1.0—92", "manual-test-v1")

    def fail(self, **kwargs):
        pytest.fail("All prior candidates must be reconciled without calling DeepSeek")

    monkeypatch.setattr("app.normatives.extraction.DeepSeekClient.complete_json", fail)
    result = client.post(f"/api/v1/documents/{doc_id}/extract-facts")
    assert result.status_code == 200, result.text
    assert result.json()["created"] == 0
    assert result.json()["deepseek_calls"] == 0
    assert result.json()["removed_self_facts"] == 1
    assert result.json()["merged_duplicates"] == 1
    saved = client.get(f"/api/v1/documents/{doc_id}/facts?fact_type=normative_reference").json()
    assert len(saved) == 3
    assert {entry["id"] for entry in saved}.isdisjoint({old_self})
    assert manual in {entry["id"] for entry in saved}
    assert {entry["id"] for entry in saved}.intersection({older, newer})
    migrated = next(entry for entry in saved if entry["value"]["code"] == "ГОСТ 30804.4.30-2013")
    assert sorted(evidence["page"] for evidence in migrated["evidence"]) == [5, 6]
    assert migrated["value"]["reference_kind"] == "normative"
    assert next(entry for entry in saved if entry["id"] == foreword)["value"]["reference_kind"] == "informative"
    assert next(entry for entry in saved if entry["id"] == manual)["value"] == {"code": "ГОСТ 1.0-92"}
    second = client.post(f"/api/v1/documents/{doc_id}/extract-facts")
    assert second.status_code == 200, second.text
    assert second.json()["merged_duplicates"] == 0
    assert second.json()["removed_self_facts"] == 0
    assert second.json()["deepseek_calls"] == 0


def test_ambiguous_editions_stay_separate(client, monkeypatch):
    blocks = [
        ExtractedBlock(3, 0, "Нормативные ссылки: ГОСТ 12345—2013 и ГОСТ 12345—2020."),
        ExtractedBlock(4, 0, "В соответствии с ГОСТ 12345 применяются измерения."),
    ]
    _, doc_id = upload(client, monkeypatch, blocks)
    monkeypatch.setattr(
        "app.normatives.extraction.DeepSeekClient.complete_json",
        lambda self, *, user_prompt, **kw: {
            "selected": [item["id"] for item in json.loads(user_prompt)["candidates"]]
        },
    )
    response = client.post(f"/api/v1/documents/{doc_id}/extract-facts")
    assert response.status_code == 200, response.text
    assert {fact["value"]["code"] for fact in response.json()["facts"]} == {
        "ГОСТ 12345-2013", "ГОСТ 12345-2020", "ГОСТ 12345"
    }


def test_reconciliation_is_rolled_back_if_model_fails(client, monkeypatch):
    blocks = [
        ExtractedBlock(1, 0, "МЕЖГОСУДАРСТВЕННЫЙ СТАНДАРТ\nГОСТ 32144—2013 Энергия"),
        ExtractedBlock(3, 0, "ГОСТ 30804.4.30—2013 применяется."),
        ExtractedBlock(4, 0, "Требования ГОСТ 23456—2020 измеряются."),
    ]
    _, doc_id = upload(client, monkeypatch, blocks)
    fragment = next(item for item in client.get(f"/api/v1/documents/{doc_id}/fragments").json()
                    if item["page"] == 1)
    old = client.post(f"/api/v1/documents/{doc_id}/facts", json={
        "fact_type": "normative_reference", "value": {"code": "ГОСТ 32144-2013"},
        "extraction_version": "deepseek-normative-v1",
        "evidence": [{"fragment_id": fragment["id"], "quote": "ГОСТ 32144—2013"}],
    })
    assert old.status_code == 201, old.text

    def unavailable(self, **kwargs):
        raise DeepSeekNotConfigured("missing")

    monkeypatch.setattr("app.normatives.extraction.DeepSeekClient.complete_json", unavailable)
    res = client.post(f"/api/v1/documents/{doc_id}/extract-facts")
    assert res.status_code == 503, res.text
    after = client.get(f"/api/v1/documents/{doc_id}/facts").json()
    assert len(after) == 1
    assert after[0]["id"] == old.json()["id"]
    assert after[0]["value"] == {"code": "ГОСТ 32144-2013"}
