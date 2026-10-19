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


def test_analyze_persists_fragment_provenance(client, monkeypatch):
    def fake_blocks(data, suffix):
        if data == b"tu-doc":
            return [
                ExtractedBlock(
                    1, 0,
                    "1.1. Максимальная мощность присоединяемых устройств 150 кВт.",
                ),
                ExtractedBlock(
                    2, 0,
                    "1.2. Установить трехфазный счетчик класса точности 0.5S.",
                ),
            ]
        return [
            ExtractedBlock(
                3, 0,
                "3.1. Предусмотреть максимальную мощность энергопринимающих "
                "устройств 150 кВт.",
            )
        ]

    monkeypatch.setattr("app.documents.router.extract_blocks", fake_blocks)
    project = client.post(
        "/api/v1/projects", json={"name": "Проект с провенансом"}
    ).json()

    files = {"tu": (b"tu-doc", "tu.pdf"), "znp": (b"znp-doc", "znp.pdf")}
    for kind, (content, filename) in files.items():
        response = client.post(
            f"/api/v1/projects/{project['id']}/documents",
            data={"kind": kind},
            files={"file": (filename, BytesIO(content), "application/pdf")},
        )
        assert response.status_code == 201, response.text

    analyzed = client.post(
        "/api/v1/requirements/analyze", json={"project_id": project["id"]}
    )
    assert analyzed.status_code == 200, analyzed.text
    data = analyzed.json()
    items = {
        item["requirement"]["clause_number"]: item
        for item in data["checked_requirements"]
    }
    assert set(items) == {"1.1", "1.2"}

    power = items["1.1"]
    assert power["status"] == "fulfilled"
    assert power["tu_page"] == 1
    assert power["tu_fragment_id"]
    assert "150 кВт" in power["tu_fragment_text"]
    assert power["znp_page"] == 3
    assert power["znp_fragment_id"]
    assert "3.1." in power["znp_fragment_text"]

    meter = items["1.2"]
    assert meter["status"] == "missing"
    assert meter["tu_page"] == 2
    assert meter["tu_fragment_id"]
    assert meter["znp_fragment_id"] is None

    # The persisted view exposes the same provenance for the UI.
    stored = client.get(
        f"/api/v1/requirements/projects/{project['id']}"
    ).json()
    stored_items = {
        item["requirement"]["clause_number"]: item
        for item in stored["checked_requirements"]
    }
    assert stored_items["1.1"]["tu_page"] == 1
    assert stored_items["1.1"]["znp_fragment_id"] == power["znp_fragment_id"]
    assert "150 кВт" in stored_items["1.1"]["znp_fragment_text"]
    assert stored_items["1.2"]["tu_page"] == 2
    assert stored_items["1.2"]["znp_fragment_id"] is None


def test_analyze_service_clauses_are_skipped(client, monkeypatch):
    def fake_blocks(data, suffix):
        if data == b"tu-doc":
            return [
                ExtractedBlock(1, 0, "Основание: заявка № 123 на присоединение."),
                ExtractedBlock(1, 1, "1.1. Максимальная мощность 150 кВт."),
            ]
        return [ExtractedBlock(1, 0, "3.1. Максимальная мощность 150 кВт.")]

    monkeypatch.setattr("app.documents.router.extract_blocks", fake_blocks)
    project = client.post(
        "/api/v1/projects", json={"name": "Проект с сервисным пунктом"}
    ).json()
    for kind, content in [("tu", b"tu-doc"), ("znp", b"znp-doc")]:
        response = client.post(
            f"/api/v1/projects/{project['id']}/documents",
            data={"kind": kind},
            files={"file": (f"{kind}.pdf", BytesIO(content), "application/pdf")},
        )
        assert response.status_code == 201, response.text

    data = client.post(
        "/api/v1/requirements/analyze", json={"project_id": project["id"]}
    ).json()
    clause_numbers = [
        item["requirement"]["clause_number"]
        for item in data["checked_requirements"]
    ]
    assert clause_numbers == ["1.1"]


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
