"""Integration checks for the persisted requirements storage layer.

AI comparison is implemented in `POST /requirements/analyze`; an empty request
is still rejected by schema validation (HTTP 422).
"""
import threading
import time
from io import BytesIO

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base, get_db
from app.core.models import Fragment
from app.documents.parser import ExtractedBlock
from app.main import app
from app.requirements.agent import RequirementsAgent
from app.requirements.extraction import Clause
from app.requirements.router import _fragment_for_quote


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


def _project_with_documents(client, monkeypatch, fake_blocks):
    monkeypatch.setattr("app.documents.router.extract_blocks", fake_blocks)
    project = client.post(
        "/api/v1/projects", json={"name": "Проект повторного анализа"}
    ).json()
    documents = {}
    for kind in ("tu", "znp"):
        response = client.post(
            f"/api/v1/projects/{project['id']}/documents",
            data={"kind": kind},
            files={"file": (f"{kind}.pdf", BytesIO(kind.encode()), "application/pdf")},
        )
        assert response.status_code == 201, response.text
        documents[kind] = response.json()["id"]
    return project["id"], documents


def _requirements_blocks(data, suffix):
    if data == b"tu":
        return [
            ExtractedBlock(1, 0, "1.1. Максимальная мощность 150 кВт."),
            ExtractedBlock(2, 0, "1.2. Установить счетчик класса точности 0.5S."),
        ]
    return [ExtractedBlock(3, 0, "3.1. Предусмотреть максимальную мощность 150 кВт.")]


def test_reanalysis_preserves_human_decisions_and_evidence_links(client, monkeypatch):
    pid, _ = _project_with_documents(client, monkeypatch, _requirements_blocks)
    base = f"/api/v1/requirements/projects/{pid}"
    first = client.post("/api/v1/requirements/analyze", json={"project_id": pid})
    assert first.status_code == 200, first.text
    first_data = first.json()
    first_items = {
        item["requirement"]["clause_number"]: item
        for item in first_data["checked_requirements"]
    }
    assert first_data["trace_info"]["analysis"] == {
        "created": 2,
        "updated": 0,
        "preserved_human": 0,
        "preserved_findings": 0,
        "removed": 0,
        "force": False,
    }

    missing = first_items["1.2"]
    findings = client.get(f"/api/v1/projects/{pid}/findings").json()
    finding = next(
        item for item in findings
        if item["type"] == "missing_requirement"
    )
    assert any(
        entry.get("requirement_id") == missing["requirement"]["id"]
        for entry in finding["evidence"]
    )

    confirmed = client.post(f"{base}/confirm", json={
        "requirement_id": missing["requirement"]["id"],
        "status": "fulfilled",
        "comment": "Инженер подтвердил требование",
    })
    assert confirmed.status_code == 200, confirmed.text

    second = client.post(
        "/api/v1/requirements/analyze", json={"project_id": pid, "force": False}
    ).json()
    second_items = {
        item["requirement"]["clause_number"]: item
        for item in second["checked_requirements"]
    }
    assert second["trace_info"]["analysis"] == {
        "created": 0,
        "updated": 2,
        "preserved_human": 1,
        "preserved_findings": 1,
        "removed": 0,
        "force": False,
    }
    assert second_items["1.1"]["requirement"]["id"] == \
        first_items["1.1"]["requirement"]["id"]
    assert second_items["1.1"]["id"] == first_items["1.1"]["id"]
    assert second_items["1.2"]["id"] == missing["id"]
    assert second_items["1.2"]["status"] == "fulfilled"
    assert second_items["1.2"]["is_confirmed_by_human"] is True
    assert second_items["1.2"]["human_override_status"] == "fulfilled"

    preserved_findings = client.get(f"/api/v1/projects/{pid}/findings").json()
    preserved = next(
        item for item in preserved_findings
        if any(
            entry.get("requirement_id") == missing["requirement"]["id"]
            for entry in item["evidence"]
        )
    )
    assert preserved["review_status"] == "rejected"

    forced = client.post(
        "/api/v1/requirements/analyze", json={"project_id": pid, "force": True}
    ).json()
    forced_items = {
        item["requirement"]["clause_number"]: item
        for item in forced["checked_requirements"]
    }
    assert forced["trace_info"]["analysis"] == {
        "created": 0,
        "updated": 2,
        "preserved_human": 0,
        "preserved_findings": 0,
        "removed": 0,
        "force": True,
    }
    assert forced_items["1.2"]["is_confirmed_by_human"] is False
    assert forced_items["1.2"]["human_override_status"] is None
    assert forced_items["1.2"]["status"] == "missing"
    reset_findings = client.get(f"/api/v1/projects/{pid}/findings").json()
    reset = next(
        item for item in reset_findings
        if any(
            entry.get("requirement_id") == missing["requirement"]["id"]
            for entry in item["evidence"]
        )
    )
    assert reset["review_status"] == "pending"


def test_confirm_updates_only_its_finding(client, monkeypatch):
    def fake_blocks(data, suffix):
        if data == b"tu":
            return [
                ExtractedBlock(1, 0, "1.1. Максимальная мощность 150 кВт."),
                ExtractedBlock(2, 0, "1.2. Установить счетчик класса точности 0.5S."),
                ExtractedBlock(3, 0, "1.3. Предусмотреть устройства РЗА."),
            ]
        return [ExtractedBlock(4, 0, "4.1. Максимальная мощность 150 кВт.")]

    pid, _ = _project_with_documents(client, monkeypatch, fake_blocks)
    analyzed = client.post(
        "/api/v1/requirements/analyze", json={"project_id": pid}
    ).json()
    items = {
        item["requirement"]["clause_number"]: item
        for item in analyzed["checked_requirements"]
    }
    assert client.post(
        f"/api/v1/requirements/projects/{pid}/confirm",
        json={
            "requirement_id": items["1.2"]["requirement"]["id"],
            "status": "fulfilled",
        },
    ).status_code == 200

    findings = client.get(f"/api/v1/projects/{pid}/findings").json()
    by_requirement = {
        entry["requirement_id"]: finding["review_status"]
        for finding in findings
        for entry in finding["evidence"]
        if entry.get("requirement_id")
    }
    assert by_requirement[items["1.2"]["requirement"]["id"]] == "rejected"
    assert by_requirement[items["1.3"]["requirement"]["id"]] == "pending"


def test_quote_maps_to_fragment_that_contains_it():
    first = Fragment(id="f1", document_id="d1", page=1, position=0, text="Начало пункта.")
    second = Fragment(id="f2", document_id="d1", page=2, position=0, text="Продолжение с цитатой 150 кВт.")
    clause = Clause(
        clause_number="1.2",
        text="Начало пункта.\nПродолжение с цитатой 150 кВт.",
        page=1,
        fragment_id="f1",
        fragment_ids=["f1", "f2"],
        section_path=None,
        fragment_text="Начало пункта.",
    )
    fragments = {"f1": first, "f2": second}
    assert _fragment_for_quote("150 кВт", clause, fragments).id == "f2"
    assert _fragment_for_quote("цитаты нет", clause, fragments).id == "f1"
    assert _fragment_for_quote("150 кВт", None, fragments) is None


def test_get_requirements_deduplicates_stable_key_and_prefers_human(client, monkeypatch):
    pid, docs = prepare(client, monkeypatch)
    base = f"/api/v1/requirements/projects/{pid}"
    payload = {
        "document_id": docs["tu"],
        "clause_number": "4.1",
        "category": "power",
        "text": "Максимальная мощность 150 кВт",
    }
    first = client.post(f"{base}/tu-requirements", json=payload).json()
    second = client.post(f"{base}/tu-requirements", json=payload).json()
    assert first["id"] != second["id"]

    assert client.post(f"{base}/confirm", json={
        "requirement_id": first["id"],
        "status": "missing",
        "comment": "Старое решение",
    }).status_code == 200
    assert client.post(f"{base}/confirm", json={
        "requirement_id": second["id"],
        "status": "fulfilled",
        "comment": "Более свежее решение",
    }).status_code == 200

    stored = client.get(base).json()
    assert len(stored["checked_requirements"]) == 1
    card = stored["checked_requirements"][0]
    assert card["requirement"]["id"] == second["id"]
    assert card["status"] == "fulfilled"
    assert card["is_confirmed_by_human"] is True
    assert card["human_comment"] == "Более свежее решение"
    assert stored["stats"] == {
        "fulfilled": 1, "missing": 0, "needs_review": 0, "total": 1,
    }


def test_reanalysis_merges_duplicates_and_preserves_human_decision(client, monkeypatch):
    pid, docs = _project_with_documents(client, monkeypatch, _requirements_blocks)
    base = f"/api/v1/requirements/projects/{pid}"
    payload = {
        "document_id": docs["tu"],
        "clause_number": "1.1",
        "category": "power",
        "text": "1.1. Максимальная мощность 150 кВт.",
    }
    first = client.post(f"{base}/tu-requirements", json=payload).json()
    duplicate = client.post(f"{base}/tu-requirements", json=payload).json()
    assert first["id"] != duplicate["id"]
    assert client.post(f"{base}/confirm", json={
        "requirement_id": first["id"],
        "status": "missing",
        "comment": "Инженерное решение",
    }).status_code == 200

    analyzed = client.post("/api/v1/requirements/analyze", json={"project_id": pid})
    assert analyzed.status_code == 200, analyzed.text
    analysis = analyzed.json()["trace_info"]["analysis"]
    assert analysis["created"] == 1
    assert analysis["updated"] == 1
    assert analysis["preserved_human"] == 1
    assert analysis["removed"] == 0

    stored_requirements = client.get(f"{base}/tu-requirements").json()
    assert sorted(item["clause_number"] for item in stored_requirements) == ["1.1", "1.2"]

    stored = client.get(base).json()
    assert len(stored["checked_requirements"]) == 2
    cards = {
        item["requirement"]["clause_number"]: item
        for item in stored["checked_requirements"]
    }
    assert set(cards) == {"1.1", "1.2"}
    assert cards["1.1"]["requirement"]["id"] == first["id"]
    assert cards["1.1"]["is_confirmed_by_human"] is True
    assert cards["1.1"]["status"] == "missing"
    assert cards["1.1"]["human_comment"] == "Инженерное решение"


def test_repeated_analysis_does_not_increase_card_count(client, monkeypatch):
    pid, _ = _project_with_documents(client, monkeypatch, _requirements_blocks)
    first = client.post("/api/v1/requirements/analyze", json={"project_id": pid})
    assert first.status_code == 200, first.text
    before = client.get(f"/api/v1/requirements/projects/{pid}").json()
    assert len(before["checked_requirements"]) == 2

    second = client.post("/api/v1/requirements/analyze", json={"project_id": pid})
    assert second.status_code == 200, second.text
    assert second.json()["trace_info"]["analysis"]["created"] == 0
    after = client.get(f"/api/v1/requirements/projects/{pid}").json()
    assert len(after["checked_requirements"]) == 2
    assert before["stats"] == after["stats"]
    assert {
        item["id"] for item in before["checked_requirements"]
    } == {
        item["id"] for item in after["checked_requirements"]
    }


def test_parallel_analysis_is_serialized(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path.as_posix()}/parallel.sqlite3",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def override():
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = override
    monkeypatch.setattr("app.documents.router.extract_blocks", _requirements_blocks)
    original_verify = RequirementsAgent.verify_all

    def slow_verify(self, tu_requirements, znp_clauses):
        # The delay happens after existing rows were read and before the commit,
        # so two overlapping requests would insert duplicates without the lock.
        time.sleep(0.2)
        return original_verify(self, tu_requirements, znp_clauses)

    monkeypatch.setattr(RequirementsAgent, "verify_all", slow_verify)
    try:
        with TestClient(app) as parallel_client:
            project = parallel_client.post(
                "/api/v1/projects", json={"name": "Проект параллельного анализа"},
            ).json()
            for kind in ("tu", "znp"):
                response = parallel_client.post(
                    f"/api/v1/projects/{project['id']}/documents",
                    data={"kind": kind},
                    files={
                        "file": (
                            f"{kind}.pdf", BytesIO(kind.encode()), "application/pdf",
                        )
                    },
                )
                assert response.status_code == 201, response.text

            barrier = threading.Barrier(2)
            responses = []

            def analyze():
                barrier.wait()
                responses.append(parallel_client.post(
                    "/api/v1/requirements/analyze",
                    json={"project_id": project["id"]},
                ))

            threads = [threading.Thread(target=analyze) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            assert [response.status_code for response in responses] == [200, 200]
            stored = parallel_client.get(
                f"/api/v1/requirements/projects/{project['id']}",
            ).json()
            assert len(stored["checked_requirements"]) == 2
            assert sorted(
                item["requirement"]["clause_number"]
                for item in stored["checked_requirements"]
            ) == ["1.1", "1.2"]
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_reanalysis_keeps_confirmed_requirement_when_text_disappears(client, monkeypatch):
    def fake_blocks(data, suffix):
        if data == b"tu2":
            return [ExtractedBlock(1, 0, "1.1. Максимальная мощность 150 кВт.")]
        return _requirements_blocks(data, suffix)

    pid, _ = _project_with_documents(client, monkeypatch, fake_blocks)
    base = f"/api/v1/requirements/projects/{pid}"
    first = client.post("/api/v1/requirements/analyze", json={"project_id": pid}).json()
    removed_item = next(
        item for item in first["checked_requirements"]
        if item["requirement"]["clause_number"] == "1.2"
    )
    assert client.post(f"{base}/confirm", json={
        "requirement_id": removed_item["requirement"]["id"],
        "status": "missing",
    }).status_code == 200

    uploaded = client.post(
        f"/api/v1/projects/{pid}/documents",
        data={"kind": "tu"},
        files={"file": ("tu2.pdf", BytesIO(b"tu2"), "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text

    second = client.post("/api/v1/requirements/analyze", json={
        "project_id": pid,
        "tu_document_id": uploaded.json()["id"],
    })
    assert second.status_code == 200, second.text
    assert second.json()["trace_info"]["analysis"]["removed"] == 1

    stored = client.get(base).json()
    stored_items = {
        item["requirement"]["clause_number"]: item
        for item in stored["checked_requirements"]
    }
    assert set(stored_items) == {"1.1", "1.2"}
    assert stored_items["1.2"]["is_confirmed_by_human"] is True
    assert stored_items["1.2"]["requirement"]["id"] == \
        removed_item["requirement"]["id"]
