"""Isolated, bounded preview for the first ten TU and ZnP statements."""
import json
from io import BytesIO

from app.documents.parser import ExtractedBlock


def project_with_clauses(client, monkeypatch, count=14):
    def blocks(data, suffix):
        kind = "TU" if data == b"tu-preview" else "ZnP"
        return [
            ExtractedBlock(
                1 + i // 6, i,
                f"{i + 1}. Для КЛ-1 предусмотреть {kind} параметр {i + 1} кВт.",
            )
            for i in range(count)
        ]

    monkeypatch.setattr("app.documents.router.extract_blocks", blocks)

    def model(self, *, system_prompt, user_prompt):
        request = json.loads(user_prompt)
        if "clause_text" in request:
            return {"atoms": []}
        if "znp_atom" in request:
            return {"links": [], "search_more": False}
        if "candidates" in request:
            return {"conflicts": [], "uncertain_ids": []}
        return {"covered": False, "evidence_ids": [], "object_consistent": False,
                "reasoning": "Нет подтверждающего фрагмента"}

    monkeypatch.setattr("app.ai.deepseek.DeepSeekClient.complete_json", model)
    pid = client.post("/api/v1/projects", json={"name": "Тест 10 × 10"}).json()["id"]
    docs = {}
    for kind, data in (("tu", b"tu-preview"), ("znp", b"znp-preview")):
        response = client.post(
            f"/api/v1/projects/{pid}/documents",
            data={"kind": kind},
            files={"file": (kind + ".pdf", BytesIO(data), "application/pdf")},
        )
        assert response.status_code == 201, response.text
        docs[kind] = response.json()["id"]
    url = f"/api/v1/projects/{pid}/comparisons"
    pairs = client.post(url, json={"pairs": [{
        "tu_document_id": docs["tu"], "znp_document_id": docs["znp"],
    }]})
    assert pairs.status_code == 201, pairs.text
    return pid, pairs.json()[0]["id"]


def test_preview_limits_both_inputs_and_stays_read_only(client, monkeypatch):
    pid, pair_id = project_with_clauses(client, monkeypatch)
    runs_url = f"/api/v1/projects/{pid}/comparisons/{pair_id}/runs"
    preview = client.post(runs_url, params={"test_mode": "true"})
    assert preview.status_code == 201, preview.text
    data = preview.json()
    result = data["result"]
    meta = result["trace_info"]["test_mode"]
    assert data["run"]["test_mode"] is True
    assert data["run"]["is_current"] is False
    assert meta["enabled"] is True and meta["isolated"] is True
    assert meta["tu_selected"] == 10
    assert meta["znp_source_clauses_selected"] == 10
    assert meta["znp_atoms_selected"] == 10
    assert result["stats"]["total"] == 10
    assert len(result["trace_info"]["znp_atom_results"]) == 10
    assert [row["requirement"]["clause_number"] for row in result["checked_requirements"]] == [
        str(i) for i in range(1, 11)
    ]
    assert [row["clause_number"] for row in result["trace_info"]["znp_atom_results"]] == [
        str(i) for i in range(1, 11)
    ]

    # The test run may be displayed, but cannot modify the project verdicts.
    base = f"/api/v1/requirements/projects/{pid}"
    assert client.get(base + "/tu-requirements").json() == []
    assert client.get(base + "/verifications").json() == []
    assert client.get(f"/api/v1/projects/{pid}/findings").json() == []
    current = client.get(f"/api/v1/projects/{pid}/comparisons/{pair_id}").json()
    assert current["current_run_id"] is None
    rejected = client.post(runs_url + "/" + data["run"]["id"] + "/confirm", json={
        "verification_id": result["checked_requirements"][0]["id"],
        "status": "fulfilled",
    })
    assert rejected.status_code == 409


def test_preview_keeps_full_run_current_and_preserves_results(client, monkeypatch):
    pid, pair_id = project_with_clauses(client, monkeypatch, count=2)
    runs_url = f"/api/v1/projects/{pid}/comparisons/{pair_id}/runs"
    full = client.post(runs_url)
    assert full.status_code == 201, full.text
    full_data = full.json()
    full_id = full_data["run"]["id"]
    assert full_data["run"]["is_current"] is True
    stored = client.get(f"/api/v1/requirements/projects/{pid}/verifications").json()
    preview = client.post(runs_url, params={"test_mode": "true"})
    assert preview.status_code == 201, preview.text
    assert preview.json()["run"]["test_mode"] is True
    assert preview.json()["run"]["is_current"] is False
    assert client.get(f"/api/v1/projects/{pid}/comparisons/{pair_id}").json()["current_run_id"] == full_id
    assert client.get(f"/api/v1/requirements/projects/{pid}/verifications").json() == stored
    assert client.get(runs_url + "/" + full_id).json()["result"] == full_data["result"]
    assert len(client.get(runs_url).json()) == 2


def test_operation_rejects_preview_batch_or_other_operation(client, monkeypatch):
    pid, pair_id = project_with_clauses(client, monkeypatch, count=1)
    url = f"/api/v1/projects/{pid}/operations"
    wrong = client.post(url, json={
        "kind": "comparison_batch", "pair_ids": [pair_id], "test_mode": True,
    })
    assert wrong.status_code == 422
    wrong = client.post(url, json={
        "kind": "normative_extract", "test_mode": True,
    })
    assert wrong.status_code == 422
