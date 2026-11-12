"""Project workspace: multiple TU/ZnP pairs, immutable run history and deletion."""
from io import BytesIO

from app.documents.parser import ExtractedBlock


def prepare(client, monkeypatch, tu_count=1, znp_count=1):
    def blocks(data, suffix):
        if data.startswith(b"tu"):
            return [ExtractedBlock(1, 0, "1.1. Предусмотреть максимальную мощность 150 кВт.")]
        return [ExtractedBlock(2, 0, "2.1. Предусмотрена максимальная мощность 150 кВт.")]

    monkeypatch.setattr("app.documents.router.extract_blocks", blocks)
    project = client.post("/api/v1/projects", json={"name": "Рабочий проект"}).json()
    ids = {"tu": [], "znp": []}
    for kind, count in (("tu", tu_count), ("znp", znp_count)):
        for number in range(count):
            response = client.post(
                f"/api/v1/projects/{project['id']}/documents",
                data={"kind": kind},
                files={"file": (
                    f"{kind}-{number}.pdf", BytesIO(kind.encode() + str(number).encode()), "application/pdf"
                )},
            )
            assert response.status_code == 201, response.text
            ids[kind].append(response.json()["id"])
    return project["id"], ids


def test_matrix_creates_many_pairs_without_duplicates(client, monkeypatch):
    project_id, ids = prepare(client, monkeypatch, tu_count=4, znp_count=5)
    pairs = [{"tu_document_id": tu, "znp_document_id": znp}
             for tu in ids["tu"] for znp in ids["znp"]][:12]
    url = f"/api/v1/projects/{project_id}/comparisons"
    created = client.post(url, json={"pairs": pairs})
    assert created.status_code == 201, created.text
    assert len(created.json()) == 12
    assert all(p["status"] == "queued" and p["run_count"] == 0 for p in created.json())
    duplicated = client.post(url, json={"pairs": pairs[:2]})
    assert duplicated.status_code == 201
    assert len(duplicated.json()) == 12
    assert len(client.get(url).json()) == 12


def test_run_history_and_deleting_one_snapshot(client, monkeypatch):
    project_id, docs = prepare(client, monkeypatch)
    url = f"/api/v1/projects/{project_id}/comparisons"
    pair = client.post(url, json={"pairs": [{
        "tu_document_id": docs["tu"][0], "znp_document_id": docs["znp"][0],
    }]}).json()[0]
    runs_url = f"{url}/{pair['id']}/runs"
    first = client.post(runs_url)
    assert first.status_code == 201, first.text
    first_id = first.json()["run"]["id"]
    assert first.json()["result"]["checked_requirements"]
    second = client.post(runs_url)
    assert second.status_code == 201, second.text
    second_id = second.json()["run"]["id"]
    assert second_id != first_id
    history = client.get(runs_url).json()
    assert len(history) == 2
    assert sum(run["is_current"] for run in history) == 1
    assert client.get(runs_url + "/" + first_id).json()["run"]["is_current"] is False
    forbidden = client.post(runs_url + "/" + first_id + "/confirm", json={
        "verification_id": first.json()["result"]["checked_requirements"][0]["id"],
        "status": "fulfilled", "comment": "старый",
    })
    assert forbidden.status_code == 409
    assert client.delete(runs_url + "/" + first_id).status_code == 204
    assert len(client.get(runs_url).json()) == 1
    assert client.get(url + "/" + pair["id"]).json()["current_run_id"] == second_id


def test_review_updates_current_snapshot_and_preserves_previous(client, monkeypatch):
    project_id, docs = prepare(client, monkeypatch)
    url = f"/api/v1/projects/{project_id}/comparisons"
    pair = client.post(url, json={"pairs": [{
        "tu_document_id": docs["tu"][0], "znp_document_id": docs["znp"][0],
    }]}).json()[0]
    runs_url = f"{url}/{pair['id']}/runs"
    first = client.post(runs_url).json()
    second = client.post(runs_url).json()
    entry = second["result"]["checked_requirements"][0]
    response = client.post(runs_url + "/" + second["run"]["id"] + "/confirm", json={
        "verification_id": entry["id"], "status": "fulfilled", "comment": "Сверено инженером",
    })
    assert response.status_code == 200, response.text
    updated = response.json()["result"]["checked_requirements"][0]
    assert updated["is_confirmed_by_human"]
    assert updated["human_comment"] == "Сверено инженером"
    old = client.get(runs_url + "/" + first["run"]["id"]).json()
    assert not old["result"]["checked_requirements"][0]["is_confirmed_by_human"]


def test_deleting_pair_does_not_delete_documents(client, monkeypatch):
    project_id, docs = prepare(client, monkeypatch)
    url = f"/api/v1/projects/{project_id}/comparisons"
    pairs = client.post(url, json={"pairs": [{
        "tu_document_id": docs["tu"][0], "znp_document_id": docs["znp"][0],
    }]}).json()
    pair_id = pairs[0]["id"]
    client.post(url + "/" + pair_id + "/runs")
    assert client.delete(url + "/" + pair_id).status_code == 204
    assert client.get(url).json() == []
    assert client.get(url + "/" + pair_id).status_code == 404
    assert len(client.get(f"/api/v1/projects/{project_id}/documents").json()) == 2


def test_cannot_compare_documents_from_other_project(client, monkeypatch):
    project_id, docs = prepare(client, monkeypatch)
    other_project_id, other_docs = prepare(client, monkeypatch)
    url = f"/api/v1/projects/{project_id}/comparisons"
    failed = client.post(url, json={"pairs": [{
        "tu_document_id": docs["tu"][0], "znp_document_id": other_docs["znp"][0],
    }]})
    assert failed.status_code == 422
    assert client.get(url).json() == []
    assert other_project_id != project_id


def test_legacy_comparison_is_visible_with_saved_result(client, monkeypatch):
    project_id, docs = prepare(client, monkeypatch)
    analyzed = client.post("/api/v1/requirements/analyze", json={
        "project_id": project_id, "tu_document_id": docs["tu"][0],
        "znp_document_id": docs["znp"][0],
    })
    assert analyzed.status_code == 200, analyzed.text
    url = f"/api/v1/projects/{project_id}/comparisons"
    listed = client.get(url)
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1
    assert listed.json()[0]["run_count"] == 1
    assert client.get(url).json()[0]["id"] == listed.json()[0]["id"]



def test_workspace_persists_deepseek_atomization_trace(client, monkeypatch):
    import json

    from app.documents.parser import ExtractedBlock

    original = ("2.1. Для КЛ-1 предусмотреть алюминиевый кабель "
                "сечением не менее 120 мм² на напряжение 10 кВ.")

    def blocks(data, suffix):
        if data == b"tu":
            return [ExtractedBlock(
                1, 0, "1.1. Для КЛ-1 предусмотреть алюминиевый кабель 120 мм², 10 кВ."
            )]
        return [ExtractedBlock(2, 0, original)]

    def answer(self, *, system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        if "clause_text" in payload:
            return {"atoms": [
                {"quote": "алюминиевый кабель",
                 "context_quotes": ["2.1. Для КЛ-1 предусмотреть"]},
                {"quote": "сечением не менее 120 мм²",
                 "context_quotes": ["2.1. Для КЛ-1 предусмотреть"]},
                {"quote": "на напряжение 10 кВ.",
                 "context_quotes": ["2.1. Для КЛ-1 предусмотреть"]},
            ]}
        if "tu_candidates" in payload:
            return {"links": [], "search_more": False}
        if "candidates" in payload:
            return {"conflicts": [], "uncertain_ids": []}
        return {"covered": False, "evidence_ids": [],
                "object_consistent": False, "reasoning": "Нет подтверждения"}

    monkeypatch.setattr("app.documents.router.extract_blocks", blocks)
    monkeypatch.setattr("app.ai.deepseek.DeepSeekClient.complete_json", answer)
    project = client.post("/api/v1/projects", json={"name": "DeepSeek atomization"}).json()
    ids = {}
    for kind in ("tu", "znp"):
        uploaded = client.post(
            f"/api/v1/projects/{project['id']}/documents",
            data={"kind": kind},
            files={"file": (f"{kind}.pdf", BytesIO(kind.encode()), "application/pdf")},
        )
        assert uploaded.status_code == 201, uploaded.text
        ids[kind] = uploaded.json()["id"]
    pairs_url = f"/api/v1/projects/{project['id']}/comparisons"
    created = client.post(pairs_url, json={"pairs": [{
        "tu_document_id": ids["tu"], "znp_document_id": ids["znp"],
    }]})
    assert created.status_code == 201, created.text
    pair = created.json()[0]
    run = client.post(f"{pairs_url}/{pair['id']}/runs")
    assert run.status_code == 201, run.text
    details = run.json()["result"]
    atomization = details["trace_info"]["atomization"]
    assert atomization["mode"] == "deepseek_conservative"
    assert atomization["clauses_total"] == 1
    assert atomization["clauses_split"] == 1
    assert atomization["atoms_total"] == 3
    assert atomization["degraded"] is False
    assert details["trace_info"]["znp_atoms_processed"] == 3
    assert details["checked_requirements"][0]["status"] == "needs_review"
    saved = client.get(f"{pairs_url}/{pair['id']}/runs/{run.json()['run']['id']}")
    assert saved.status_code == 200
    assert saved.json()["result"]["trace_info"]["atomization"] == atomization
