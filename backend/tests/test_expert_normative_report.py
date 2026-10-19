"""Expert normative report tests: database snapshots, citations, expert reviews."""
from datetime import datetime, timezone
from io import BytesIO

from app.documents.parser import ExtractedBlock
from app.normatives.registry import RegistryResult


def prepare(client, monkeypatch):
    monkeypatch.setattr(
        "app.documents.router.extract_blocks",
        lambda data, suffix: [
            ExtractedBlock(1, 0, "ГОСТ 29322—92 использован в требованиях"),
            ExtractedBlock(2, 0, "В предисловии ГОСТ 1.0—92 упомянут."),
            ExtractedBlock(3, 0, "Нормативный ГОСТ 30804.4.30—2013 указан здесь."),
            ExtractedBlock(4, 0, "IEC 61000-4-30:2008 также используется."),
        ],
    )
    project = client.post("/api/v1/projects", json={"name": "Тест отчёта"}).json()
    resp = client.post(
        f"/api/v1/projects/{project['id']}/documents",
        data={"kind": "other"},
        files={"file": ("test.pdf", BytesIO(b"fake pdf"), "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    doc_id = resp.json()["id"]
    frag = {
        entry["page"]: entry["id"]
        for entry in client.get(f"/api/v1/documents/{doc_id}/fragments").json()
    }
    for code, quote, page, reference_kind in [
        ("ГОСТ 29322-92", "ГОСТ 29322—92", 1, "normative"),
        ("ГОСТ 1.0-92", "ГОСТ 1.0—92", 2, "informative"),
        ("ГОСТ 30804.4.30-2013", "ГОСТ 30804.4.30—2013", 3, "normative"),
        ("IEC 61000-4-30:2008", "IEC 61000-4-30:2008", 4, "normative"),
    ]:
        res = client.post(f"/api/v1/documents/{doc_id}/facts", json={
            "fact_type": "normative_reference", "value": {
                "code": code, "reference_kind": reference_kind,
            },
            "evidence": [{"fragment_id": frag[page], "quote": quote}],
        })
        assert res.status_code == 201, res.text
    return project["id"], doc_id


def responses(self, code, source_url=None):
    if code == "ГОСТ 29322-92":
        return RegistryResult(
            code=code, status="obsolete", registry_status="Заменен",
            title="Стандартные напряжения", replacement="ГОСТ 29322-2014",
            source_url="https://protect.gost.ru/gost/details/ddd080ed-1dd3-41bd-8cf7-f55a18dc2c5e",
            checked_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
        )
    if code == "ГОСТ 1.0-92":
        return RegistryResult(
            code=code, status="obsolete", registry_status="Заменен",
            title="Основные положения", replacement="ГОСТ 1.0-2015",
            source_url="https://protect.gost.ru/gost/details/2a59036c-9b31-4130-b962-67a89db23f4d",
            checked_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
        )
    if code == "ГОСТ 30804.4.30-2013":
        return RegistryResult(
            code=code, status="active", registry_status="Действует",
            title="Методы измерений",
            source_url="https://protect.gost.ru/gost/details/cb842376-45fb-46fe-82d7-5f66960f6f87",
            checked_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
        )
    return RegistryResult(code=code, reason="unsupported_or_incomplete_designation")


def test_report_grouping_persistence_provenance_and_review(client, monkeypatch):
    project_id, doc_id = prepare(client, monkeypatch)
    url = f"/api/v1/projects/{project_id}/normative-report"
    assert client.get(url).status_code == 200
    before = client.get(url).json()
    assert before["total"] == 4
    assert before["unverified"] == 4
    assert all(item["checked_at"] is None for item in before["items"])
    assert all(item["reason"] == "not_yet_verified" for item in before["items"])

    monkeypatch.setattr("app.normatives.registry.RosstandartClient.lookup", responses)
    checked = client.post(f"/api/v1/documents/{doc_id}/check-normatives")
    assert checked.status_code == 200, checked.text
    report = client.get(url).json()
    assert (report["total"], report["requires_attention"], report["informative"],
            report["unverified"], report["active"]) == (4, 1, 1, 1, 1)
    by_code = {item["code"]: item for item in report["items"]}
    normative = by_code["ГОСТ 29322-92"]
    assert normative["section"] == "requires_attention"
    assert normative["source_url"].startswith("https://protect.gost.ru/gost/details/")
    assert normative["checked_at"].startswith("2026-10-19")
    assert normative["attempted_at"] is not None
    assert normative["replacement"] == "ГОСТ 29322-2014"
    assert normative["evidence"][0]["quote"] == "ГОСТ 29322—92"
    assert normative["evidence"][0]["page"] == 1
    assert normative["document_id"] == doc_id
    assert normative["document_name"] == "test.pdf"
    assert normative["finding"]["id"]
    assert normative["finding"]["review_status"] == "pending"
    informative = by_code["ГОСТ 1.0-92"]
    assert informative["section"] == "informative"
    assert informative["finding"]["severity"] == "info"
    assert by_code["ГОСТ 30804.4.30-2013"]["section"] == "active"
    international = by_code["IEC 61000-4-30:2008"]
    assert international["section"] == "unverified"
    assert international["checked_at"] is None
    assert international["attempted_at"] is not None
    assert international["reason"] == "unsupported_or_incomplete_designation"
    assert international["source_url"] is None

    review_id = normative["finding"]["id"]
    res = client.patch(f"/api/v1/findings/{review_id}/review",
                       json={"review_status": "accepted"})
    assert res.status_code == 200
    after_review = client.get(url).json()
    reviewed = next(i for i in after_review["items"] if i["code"] == "ГОСТ 29322-92")
    assert reviewed["finding"]["review_status"] == "accepted"

    again = client.post(f"/api/v1/documents/{doc_id}/check-normatives")
    assert again.status_code == 200
    assert again.json()["findings_created"] == 0
    persisted = client.get(url).json()
    assert persisted["total"] == 4
    assert next(i for i in persisted["items"] if i["code"] == "ГОСТ 29322-92")["finding"]["review_status"] == "accepted"


def test_unknown_refresh_overrides_earlier_success_without_forging_status(client, monkeypatch):
    project_id, doc_id = prepare(client, monkeypatch)
    monkeypatch.setattr("app.normatives.registry.RosstandartClient.lookup", responses)
    assert client.post(f"/api/v1/documents/{doc_id}/check-normatives").status_code == 200

    def inaccessible(self, code, source_url=None):
        return RegistryResult(code=code, reason="official_verification_unavailable")

    monkeypatch.setattr("app.normatives.registry.RosstandartClient.lookup", inaccessible)
    assert client.post(f"/api/v1/documents/{doc_id}/check-normatives").status_code == 200
    report = client.get(f"/api/v1/projects/{project_id}/normative-report").json()
    assert report["unverified"] == 4
    assert report["requires_attention"] == 0
    assert report["active"] == 0
    for item in report["items"]:
        assert item["status"] == "unknown"
        assert item["checked_at"] is None
        assert item["source_url"] is None
        assert item["reason"] == "official_verification_unavailable"

    # Findings remain in the audit trail, even if the live registry is offline.
    findings = client.get(f"/api/v1/projects/{project_id}/findings").json()
    assert len(findings) == 2


def test_empty_project_and_not_found(client):
    missing = client.get("/api/v1/projects/no-such-id/normative-report")
    assert missing.status_code == 404
    project = client.post("/api/v1/projects", json={"name": "Пустой проект"}).json()
    res = client.get(f"/api/v1/projects/{project['id']}/normative-report")
    assert res.status_code == 200
    assert res.json()["total"] == 0
    assert res.json()["items"] == []


def test_report_filters_one_document_without_mixing_other_uploads(client, monkeypatch):
    project_id, first_id = prepare(client, monkeypatch)
    monkeypatch.setattr("app.normatives.registry.RosstandartClient.lookup", responses)
    checked = client.post(f"/api/v1/documents/{first_id}/check-normatives")
    assert checked.status_code == 200, checked.text

    duplicate = client.post(
        f"/api/v1/projects/{project_id}/documents",
        data={"kind": "other"},
        files={"file": ("test.pdf", BytesIO(b"second copy"), "application/pdf")},
    )
    assert duplicate.status_code == 201, duplicate.text
    second_id = duplicate.json()["id"]
    frag = next(item for item in client.get(
        f"/api/v1/documents/{second_id}/fragments").json() if item["page"] == 1)
    fact = client.post(f"/api/v1/documents/{second_id}/facts", json={
        "fact_type": "normative_reference",
        "value": {"code": "ГОСТ 29322-92", "reference_kind": "normative"},
        "evidence": [{"fragment_id": frag["id"], "quote": "ГОСТ 29322—92"}],
    })
    assert fact.status_code == 201, fact.text

    endpoint = f"/api/v1/projects/{project_id}/normative-report"
    combined = client.get(endpoint)
    assert combined.status_code == 200
    assert combined.json()["total"] == 5
    assert combined.json()["selected_document_id"] is None

    first = client.get(endpoint, params={"document_id": first_id})
    assert first.status_code == 200, first.text
    first_report = first.json()
    assert first_report["selected_document_id"] == first_id
    assert (first_report["total"], first_report["requires_attention"],
            first_report["informative"], first_report["unverified"],
            first_report["active"]) == (4, 1, 1, 1, 1)
    assert {item["document_id"] for item in first_report["items"]} == {first_id}
    assert next(x for x in first_report["items"] if x["code"] == "ГОСТ 29322-92")["finding"]

    second = client.get(endpoint, params={"document_id": second_id})
    assert second.status_code == 200, second.text
    second_report = second.json()
    assert second_report["selected_document_id"] == second_id
    assert second_report["total"] == second_report["unverified"] == 1
    assert second_report["requires_attention"] == 0
    assert second_report["items"][0]["status"] == "unknown"
    assert second_report["items"][0]["finding"] is None
    assert second_report["items"][0]["source_url"] is None
    assert second_report["items"][0]["reason"] == "not_yet_verified"
    assert second_report["items"][0]["evidence"][0]["fragment_id"] == frag["id"]
    assert client.get(endpoint, params={"document_id": "invalid"}).status_code == 404

    another = client.post("/api/v1/projects", json={"name": "Another"}).json()
    other_endpoint = f"/api/v1/projects/{another['id']}/normative-report"
    assert client.get(other_endpoint, params={"document_id": first_id}).status_code == 404
    assert client.get(other_endpoint).json()["total"] == 0
