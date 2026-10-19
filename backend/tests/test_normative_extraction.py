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
    assert all(f["extraction_version"] == "deepseek-normative-v1" for f in data["facts"])
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
