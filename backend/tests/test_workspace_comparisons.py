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
