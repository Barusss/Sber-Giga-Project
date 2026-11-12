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
from app.requirements.router import _fragment_for_quote, _apply_quote_provenance
from app.requirements.schemas import RequirementCheckItem, RequirementStatus, TURequirement, RequirementCategory, ZNPClause


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
    assert power["status"] == "needs_review"
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


def test_analyze_skips_headings_toc_and_service_sections(client, monkeypatch):
    def fake_blocks(data, suffix):
        if data == b"tu-doc":
            return [
                ExtractedBlock(1, 0, "ТЕХНИЧЕСКИЕ УСЛОВИЯ"),
                ExtractedBlock(
                    1, 1, "Содержание", block_type="heading",
                    section_path="Содержание",
                ),
                ExtractedBlock(1, 2, "1. Общие положения ........ 3"),
                ExtractedBlock(
                    1, 3, "1. Общие положения", block_type="heading",
                    section_path="1. Общие положения",
                ),
                ExtractedBlock(1, 4, "1.1. Максимальная мощность 150 кВт."),
                ExtractedBlock(
                    2, 0, "2. Характеристика", block_type="heading",
                    section_path="2. Характеристика",
                ),
                ExtractedBlock(2, 1, "Установить счетчик класса точности 0.5S."),
                ExtractedBlock(
                    3, 0, "Приложение А. Перечень сокращений",
                    block_type="heading",
                    section_path="Приложение А. Перечень сокращений",
                ),
                ExtractedBlock(
                    3, 1, "Сокращение | Расшифровка", block_type="table_row",
                    section_path="Приложение А. Перечень сокращений",
                ),
            ]
        return [ExtractedBlock(3, 0, "3.1. Максимальная мощность 150 кВт.")]

    monkeypatch.setattr("app.documents.router.extract_blocks", fake_blocks)
    project = client.post(
        "/api/v1/projects", json={"name": "Проект без заголовков в требованиях"}
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
    items = {
        item["requirement"]["clause_number"]: item
        for item in data["checked_requirements"]
    }

    assert set(items) == {"1.1", "2"}
    assert items["1.1"]["status"] == "needs_review"
    assert items["2"]["status"] == "missing"
    assert items["2"]["requirement"]["text"] == \
        "Установить счетчик класса точности 0.5S."
    assert data["stats"]["total"] == 2
    assert all(
        "Содержание" not in item["requirement"]["text"]
        and "Приложение" not in item["requirement"]["text"]
        for item in items.values()
    )


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
    pid, docs = _project_with_documents(client, monkeypatch, _requirements_blocks)
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

    assert client.post(f"/api/v1/projects/{pid}/approve").status_code == 409
    analyzed = client.post("/api/v1/requirements/analyze", json={"project_id": pid}).json()
    for card in analyzed["checked_requirements"]:
        assert client.post(f"{base}/confirm", json={"requirement_id": card["requirement"]["id"],
            "verification_id": card["id"], "status": "fulfilled"}).status_code == 200
    approval = client.post(f"/api/v1/projects/{pid}/approve")
    assert approval.status_code == 200, approval.text
    assert client.post(f"{base}/tu-requirements", json=payload).status_code == 409
    assert client.post(f"{base}/confirm", json={
        "requirement_id": requirement_id, "status": "fulfilled",
    }).status_code == 409
    # A caller must not be allowed to swap document roles just by IDs.
    assert client.post("/api/v1/requirements/analyze", json={
        "project_id": pid, "tu_document_id": docs["znp"],
        "znp_document_id": docs["tu"],
    }).status_code == 409  # Project is approved above.
    assert client.get(f"{base}/tu-requirements").json()
    assert client.get(f"{base}/verifications").json()


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


def test_expert_report_includes_complete_saved_pair_and_normatives(client, monkeypatch):
    pid, docs = _project_with_documents(client, monkeypatch, _requirements_blocks)
    analyzed = client.post("/api/v1/requirements/analyze", json={"project_id": pid})
    assert analyzed.status_code == 200, analyzed.text
    response = client.get(f"/api/v1/projects/{pid}/expert-report")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["requirements"]["pair"]["tu_document_id"] == docs["tu"]
    assert data["requirements"]["pair"]["znp_document_id"] == docs["znp"]
    assert data["requirements"]["complete"] is True
    assert len(data["requirements"]["items"]) == data["requirements"]["stats"]["total"] == 2
    assert "normatives" in data


def test_expert_report_requires_choice_for_multiple_pairs(client, monkeypatch):
    pid, docs = _project_with_documents(client, monkeypatch, _requirements_blocks)
    response = client.post(f"/api/v1/projects/{pid}/documents", data={"kind": "znp"},
        files={"file": ("second.pdf", BytesIO(b"znp"), "application/pdf")})
    assert response.status_code == 201, response.text
    second_znp = response.json()["id"]
    for znp in (docs["znp"], second_znp):
        analyzed = client.post("/api/v1/requirements/analyze", json={"project_id": pid,
            "tu_document_id": docs["tu"], "znp_document_id": znp})
        assert analyzed.status_code == 200, analyzed.text
    url = f"/api/v1/projects/{pid}/expert-report"
    report = client.get(url).json()["requirements"]
    assert len(report["available_pairs"]) == 2
    assert report["pair"] is None
    chosen = client.get(url, params={"tu_document_id": docs["tu"], "znp_document_id": second_znp})
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["requirements"]["pair"]["znp_document_id"] == second_znp
    assert client.get(url, params={"tu_document_id": docs["tu"], "znp_document_id": "unrelated"}).status_code == 404


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
    assert _fragment_for_quote("цитаты нет", clause, fragments) is None
    assert _fragment_for_quote("150 кВт", None, fragments) is None


def test_list_evidence_must_map_to_a_real_fragment():
    fragment = Fragment(id="f", document_id="znp-doc", page=2, position=0, text="Настоящий текст")
    clause = Clause(clause_number="1", text="Настоящий текст и отсутствующая цитата", page=2,
                    fragment_id="f", fragment_ids=["f"], section_path=None, fragment_text=fragment.text)
    requirement = TURequirement(id="tu", clause_number="1", category=RequirementCategory.other, text="Требование")
    item = RequirementCheckItem(id="v", requirement=requirement, status=RequirementStatus.fulfilled,
        confidence_score=0.9, matched_clause=ZNPClause(id="znp-1", clause_number="1", text=clause.text),
        validation_details={"version": 1, "items": [{"item_id": 1, "status": "fulfilled", "evidence": [
            {"clause_id": "znp-1", "quote": "отсутствующая цитата"}]}]})
    _apply_quote_provenance([item], {}, {"znp-1": clause}, {"f": fragment}, "znp-doc")
    assert item.status == RequirementStatus.needs_review
    assert item.validation_details["items"][0]["evidence"] == []


def test_list_evidence_is_persisted_with_source_fragments(client, monkeypatch):
    def blocks(data, suffix):
        if data == b"tu":
            return [ExtractedBlock(1, 0, "1.1. Предусмотреть:\n- устройство АБ-123;\n- устройство ВГ-456;")]
        return [ExtractedBlock(2, 0, "2.1. Установить устройство АБ-123."),
                ExtractedBlock(3, 0, "2.2. Установить устройство ВГ-456.")]
    pid, docs = _project_with_documents(client, monkeypatch, blocks)
    def reply(self, system_prompt, user_prompt):
        return {"action": "finish", "status": "fulfilled", "confidence_score": 0.95,
            "tu_quote": "устройство АБ-123", "reasoning": "Подтверждено",
            "evidence_items": [
                {"item_id": 1, "status": "fulfilled", "evidence": [{"clause_id": "znp-1", "quote": "устройство АБ-123"}]},
                {"item_id": 2, "status": "fulfilled", "evidence": [{"clause_id": "znp-2", "quote": "устройство ВГ-456"}]},
            ]}
    monkeypatch.setattr("app.ai.deepseek.DeepSeekClient.complete_json", reply)
    response = client.post("/api/v1/requirements/analyze", json={"project_id": pid})
    assert response.status_code == 200, response.text
    item = response.json()["checked_requirements"][0]
    assert item["status"] == "fulfilled"
    details = item["validation_details"]["items"]
    assert len(details) == 2
    assert all(part["evidence"][0]["document_id"] == docs["znp"] for part in details)
    assert len({part["evidence"][0]["fragment_id"] for part in details}) == 2
    stored = client.get(f"/api/v1/requirements/projects/{pid}").json()["checked_requirements"][0]
    assert stored["validation_details"]["items"] == details


def test_single_evidence_spanning_fragments_is_not_misattributed():
    first = Fragment(id="f1", document_id="znp-doc", page=1, position=0, text="Часть первая")
    second = Fragment(id="f2", document_id="znp-doc", page=2, position=0, text="часть вторая")
    clause = Clause(clause_number="1", text="Часть первая часть вторая", page=1,
        fragment_id="f1", fragment_ids=["f1", "f2"], section_path=None, fragment_text=first.text)
    requirement = TURequirement(id="tu", clause_number="1", category=RequirementCategory.other, text="Требование")
    item = RequirementCheckItem(id="v", requirement=requirement, status=RequirementStatus.fulfilled,
        confidence_score=0.9, znp_quote="Часть первая часть вторая",
        matched_clause=ZNPClause(id="znp-1", clause_number="1", text=clause.text))
    _apply_quote_provenance([item], {}, {"znp-1": clause}, {"f1": first, "f2": second}, "znp-doc")
    assert item.status == RequirementStatus.needs_review
    assert item.znp_fragment_id is None
    assert item.znp_quote is None


def test_multi_citation_element_is_not_fulfilled_after_losing_one_source():
    first = Fragment(id="f1", document_id="znp-doc", page=1, position=0, text="Устройство АБ-123: 150 кВт")
    second = Fragment(id="f2", document_id="znp-doc", page=2, position=0, text="Нет нужной цитаты")
    clauses = {
        "z1": Clause(clause_number="1", text=first.text, page=1, fragment_id="f1", fragment_ids=["f1"], section_path=None, fragment_text=first.text),
        "z2": Clause(clause_number="2", text="Устройство АБ-123: 110 кВ", page=2, fragment_id="f2", fragment_ids=["f2"], section_path=None, fragment_text=second.text),
    }
    requirement = TURequirement(id="tu", clause_number="1", category=RequirementCategory.other, text="Требование")
    item = RequirementCheckItem(id="v", requirement=requirement, status=RequirementStatus.fulfilled, confidence_score=0.9,
        matched_clause=ZNPClause(id="z1", clause_number="1", text=first.text),
        validation_details={"version": 1, "items": [{"item_id": 1, "status": "fulfilled", "evidence": [
            {"clause_id": "z1", "quote": first.text}, {"clause_id": "z2", "quote": "Устройство АБ-123: 110 кВ"}]}]})
    _apply_quote_provenance([item], {}, clauses, {"f1": first, "f2": second}, "znp-doc")
    assert len(item.validation_details["items"][0]["evidence"]) == 1
    assert item.validation_details["items"][0]["status"] == "needs_review"
    assert item.status == RequirementStatus.needs_review


def test_missing_list_result_keeps_consistent_reason_after_provenance():
    requirement = TURequirement(id="tu", clause_number="1", category=RequirementCategory.other, text="Требование")
    item = RequirementCheckItem(id="v", requirement=requirement, status=RequirementStatus.missing,
        confidence_score=0.9, reasoning="Модель не установила соответствия.",
        validation_details={"version": 1, "items": [{"item_id": 1, "status": "needs_review", "evidence": []}]})
    _apply_quote_provenance([item], {}, {}, {}, "znp-doc")
    assert item.status == RequirementStatus.missing
    assert "Требует проверки" not in item.reasoning


def test_list_provenance_preserves_missing_tu_quote_warning():
    fragment = Fragment(id="f", document_id="tu-doc", page=1, position=0, text="Начало требования")
    clause = Clause(clause_number="1", text="Начало требования продолжение", page=1,
        fragment_id="f", fragment_ids=["f"], section_path=None, fragment_text=fragment.text)
    requirement = TURequirement(id="tu", clause_number="1", category=RequirementCategory.other, text=clause.text)
    item = RequirementCheckItem(id="v", requirement=requirement, status=RequirementStatus.fulfilled,
        confidence_score=0.9, tu_quote="Начало требования продолжение",
        validation_details={"version": 1, "items": [{"item_id": 1, "status": "needs_review", "evidence": []}]})
    _apply_quote_provenance([item], {"tu": clause}, {}, {"f": fragment}, "znp-doc")
    assert item.status == RequirementStatus.needs_review
    assert "цитата ТУ" in item.reasoning


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
        "znp_document_id": docs["znp"],
    }).status_code == 200

    analyzed = client.post("/api/v1/requirements/analyze", json={"project_id": pid})
    assert analyzed.status_code == 200, analyzed.text
    analysis = analyzed.json()["trace_info"]["analysis"]
    assert analysis["created"] == 1
    assert analysis["updated"] == 1
    assert analysis["preserved_human"] == 1
    assert analysis["removed"] == 1

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
    assert second.json()["trace_info"]["analysis"]["removed"] == 0

    stored = client.get(base).json()
    stored_items = {
        item["requirement"]["clause_number"]: item
        for item in stored["checked_requirements"]
    }
    assert set(stored_items) == {"1.1", "1.2"}
    assert stored_items["1.2"]["is_confirmed_by_human"] is True
    assert stored_items["1.2"]["requirement"]["id"] == \
        removed_item["requirement"]["id"]


def test_explicit_document_roles_are_validated_before_analysis(client, monkeypatch):
    pid, docs = prepare(client, monkeypatch)
    endpoint = "/api/v1/requirements/analyze"
    assert client.post(endpoint, json={
        "project_id": pid,
        "tu_document_id": docs["znp"],
        "znp_document_id": docs["tu"],
    }).status_code == 422
    assert client.post(endpoint, json={
        "project_id": pid,
        "tu_document_id": docs["tu"],
        "znp_document_id": docs["tu"],
    }).status_code == 422


def test_validation_details_survive_read_and_confirmed_reanalysis(client, monkeypatch):
    pid, _ = _project_with_documents(client, monkeypatch, _requirements_blocks)
    original_verify = RequirementsAgent.verify_all
    details = {"version": 1, "model_reasoning": "Исходный вывод модели"}

    def with_details(self, tu_requirements, znp_clauses):
        result = original_verify(self, tu_requirements, znp_clauses)
        result.checked_requirements[0].validation_details = details
        return result

    monkeypatch.setattr(RequirementsAgent, "verify_all", with_details)
    endpoint = "/api/v1/requirements/analyze"
    response = client.post(endpoint, json={"project_id": pid})
    assert response.status_code == 200, response.text
    item = response.json()["checked_requirements"][0]
    base = f"/api/v1/requirements/projects/{pid}"
    stored = client.get(base).json()["checked_requirements"]
    assert next(x for x in stored if x["id"] == item["id"])["validation_details"] == details
    confirmed = client.post(f"{base}/confirm", json={
        "requirement_id": item["requirement"]["id"],
        "verification_id": item["id"], "status": "fulfilled",
    })
    assert confirmed.status_code == 200, confirmed.text
    details = {"version": 1, "model_reasoning": "Новый вывод"}
    repeated = client.post(endpoint, json={"project_id": pid})
    assert repeated.status_code == 200, repeated.text
    kept = next(x for x in repeated.json()["checked_requirements"] if x["id"] == item["id"])
    assert kept["validation_details"]["model_reasoning"] == "Исходный вывод модели"


def test_reanalysis_keeps_confirmed_evidence_and_source_when_new_match_changes(client, monkeypatch):
    pid, _ = _project_with_documents(client, monkeypatch, _requirements_blocks)
    endpoint = "/api/v1/requirements/analyze"
    first = client.post(endpoint, json={"project_id": pid})
    assert first.status_code == 200, first.text
    old = next(
        item for item in first.json()["checked_requirements"]
        if item["requirement"]["clause_number"] == "1.1"
    )
    assert old["matched_clause"] is not None
    assert old["znp_quote"]
    assert old["znp_fragment_id"]

    confirmed = client.post(f"/api/v1/requirements/projects/{pid}/confirm", json={
        "requirement_id": old["requirement"]["id"],
        "status": "fulfilled",
        "comment": "Проверено по исходной цитате",
    })
    assert confirmed.status_code == 200, confirmed.text

    original_verify = RequirementsAgent.verify_all

    def changed_match(self, tu_requirements, znp_clauses):
        result = original_verify(self, tu_requirements, znp_clauses)
        for item in result.checked_requirements:
            if item.requirement.clause_number == "1.1":
                item.matched_clause = None
                item.znp_quote = None
                item.znp_fragment_id = None
                item.znp_page = None
                item.znp_fragment_text = None
                item.reasoning = "Новая модель не нашла совпадений"
        return result

    monkeypatch.setattr(RequirementsAgent, "verify_all", changed_match)
    repeated = client.post(endpoint, json={"project_id": pid})
    assert repeated.status_code == 200, repeated.text
    kept = next(
        item for item in repeated.json()["checked_requirements"]
        if item["requirement"]["id"] == old["requirement"]["id"]
    )
    assert kept["is_confirmed_by_human"] is True
    assert kept["human_comment"] == "Проверено по исходной цитате"
    assert kept["status"] == "fulfilled"
    assert kept["automatic_status"] == old["status"]
    assert kept["matched_clause"]["text"] == old["matched_clause"]["text"]
    assert kept["znp_quote"] == old["znp_quote"]
    assert kept["znp_fragment_id"] == old["znp_fragment_id"]
    assert kept["znp_page"] == old["znp_page"]

    persisted = client.get(f"/api/v1/requirements/projects/{pid}").json()
    stored = next(
        item for item in persisted["checked_requirements"]
        if item["requirement"]["id"] == old["requirement"]["id"]
    )
    assert stored["matched_clause"]["text"] == old["matched_clause"]["text"]
    assert stored["automatic_status"] == old["status"]
    assert stored["znp_quote"] == old["znp_quote"]
    assert stored["znp_fragment_id"] == old["znp_fragment_id"]
