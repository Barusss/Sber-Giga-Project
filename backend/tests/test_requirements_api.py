"""Integration checks for the persisted requirements storage layer.

AI comparison is implemented in `POST /requirements/analyze`; an empty request
is still rejected by schema validation (HTTP 422).
"""
from io import BytesIO

from app.documents.parser import ExtractedBlock


def prepare(client, monkeypatch):
    monkeypatch.setattr(
        "app.documents.router.extract_blocks",
        lambda data, suffix: [ExtractedBlock(1, 0, "Установить мощность 150 кВт")],
    )
    project = client.post("/api/v1/projects", json={"name": "Проверка интеграции DPazY"}).json()
    documents = {}
    for kind in ["tu", "znp"]:
        response = client.post(
            f"/api/v1/projects/{project['id']}/documents",
            data={"kind": kind},
            files={"file": (f"{kind}.pdf", BytesIO(b"PDF mock bytes"), "application/pdf")},
        )
        assert response.status_code == 201, response.text
        documents[kind] = response.json()["id"]
    return project["id"], documents


def test_manual_tu_storage_and_human_verdict(client, monkeypatch):
    pid, docs = prepare(client, monkeypatch)
    base = f"/api/v1/requirements/projects/{pid}"
    payload = {
        "document_id": docs["tu"],
        "clause_number": "п. 4.1",
        "category": "power",
        "text": "Максимальная мощность 150 кВт",
    }
    created = client.post(f"{base}/tu-requirements", json=payload)
    assert created.status_code == 201, created.text
    saved = created.json()
    assert saved["category"] == "power"
    assert saved["document_id"] == docs["tu"]
    assert saved["project_id"] == pid
    assert saved["id"]
    assert saved["created_at"]

    all_rows = client.get(f"{base}/tu-requirements")
    assert all_rows.status_code == 200, all_rows.text
    assert len(all_rows.json()) == 1
    filtered = client.get(f"{base}/tu-requirements", params={"document_id": docs["tu"]})
    assert [r["id"] for r in filtered.json()] == [saved["id"]]
    assert client.get(f"{base}/tu-requirements",
                      params={"document_id": docs["znp"]}).json() == []
    assert client.get(f"{base}/verifications").json() == []

    # Human decision is NOT an AI finding; it only populates the DPazY HITL model.
    confirmation = {
        "requirement_id": saved["id"],
        "status": "fulfilled",
        "comment": "Согласовано главным энергетиком",
    }
    first = client.post(f"{base}/confirm", json=confirmation)
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["is_confirmed_by_human"] is True
    assert result["status"] == "needs_review"
    assert result["human_override_status"] == "fulfilled"
    assert result["confidence_score"] is None
    assert result["human_comment"] == confirmation["comment"]

    confirmation["status"] = "missing"
    confirmation["comment"] = "ЗнП не содержит требование"
    second = client.post(f"{base}/confirm", json=confirmation)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == result["id"]
    assert second.json()["human_override_status"] == "missing"
    assert len(client.get(f"{base}/verifications").json()) == 1
    assert client.get(f"/api/v1/projects/{pid}/findings").json() == []
    assert client.post("/api/v1/requirements/analyze").status_code == 422


def test_project_isolation_type_and_review_protection(client, monkeypatch):
    pid, docs = prepare(client, monkeypatch)
    other = client.post("/api/v1/projects", json={"name": "Другой проект"}).json()["id"]
    base = f"/api/v1/requirements/projects/{pid}"
    payload = {
        "document_id": docs["tu"], "clause_number": "4.2", "category": "power",
        "text": "150 кВт",
    }
    assert client.post(f"/api/v1/requirements/projects/{other}/tu-requirements",
                       json=payload).status_code == 404
    assert client.post(f"{base}/tu-requirements", json={**payload, "document_id": docs["znp"]}).status_code == 422
    assert client.post(f"{base}/tu-requirements", json={**payload, "category": "nope"}).status_code == 422
    assert client.post(f"{base}/tu-requirements", json={**payload, "text": "  "}).status_code == 422
    assert client.get(f"/api/v1/requirements/projects/{other}/tu-requirements",
                      params={"document_id": docs["tu"]}).status_code == 404

    create = client.post(f"{base}/tu-requirements", json=payload)
    assert create.status_code == 201
    requirement_id = create.json()["id"]
    assert client.post(f"/api/v1/requirements/projects/{other}/confirm", json={
        "requirement_id": requirement_id, "status": "fulfilled",
    }).status_code == 404
    assert client.post(f"{base}/confirm", json={
        "requirement_id": "unknown", "status": "fulfilled",
    }).status_code == 404

    approval = client.post(f"/api/v1/projects/{pid}/approve")
    assert approval.status_code == 200, approval.text
    assert client.post(f"{base}/tu-requirements", json=payload).status_code == 409
    assert client.post(f"{base}/confirm", json={
        "requirement_id": requirement_id, "status": "fulfilled",
    }).status_code == 409
    assert len(client.get(f"{base}/tu-requirements").json()) == 1
    assert client.get(f"{base}/verifications").json() == []


def test_core_legacy_services_still_visible(client):
    assert client.get("/api/v1/normatives").status_code == 200
    assert client.get("/api/v1/projects").status_code == 200
    assert client.post("/api/v1/requirements/analyze").status_code == 422
