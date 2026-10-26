"""Official registry verification: mocked HTTP; never depends on live website."""
from datetime import datetime, timezone
from io import BytesIO

import httpx
import pytest

from app.documents.parser import ExtractedBlock
from app.normatives.registry import (
    RegistryResult, RosstandartClient, detail_path, parse_detail, parse_search,
)

OFFICIAL_ID = "ddd080ed-1dd3-41bd-8cf7-f55a18dc2c5e"
CARD_URL = f"https://protect.gost.ru/gost/details/{OFFICIAL_ID}"


def card(code="ГОСТ 29322-92", status="Заменен", replacement="ГОСТ 29322-2014"):
    return f"""
    <html><body><h1>{code}</h1>
    <div>Стандартные напряжения</div><span>{status}</span>
    <h3>Основные сведения</h3>
    <div>Обозначение</div><span>{code}</span>
    <div>Полное обозначение</div><span>{code}</span>
    <div>Заглавие на русском языке</div><span>Стандартные напряжения</span>
    <h3>Ссылочные данные</h3>
    <div>Обозначение заменяющего</div><span>{replacement}</span>
    </body></html>"""


def test_parse_official_card_and_status():
    result = parse_detail(card(), "ГОСТ 29322-92", CARD_URL)
    assert result.status == "obsolete"
    assert result.registry_status == "Заменен"
    assert result.replacement == "ГОСТ 29322-2014"
    assert result.title == "Стандартные напряжения"
    assert result.checked_at
    active = parse_detail(card(status="Действует", replacement="-"), "ГОСТ 29322-92", CARD_URL)
    assert active.status == "active"
    assert active.replacement is None


def test_exact_official_lookup_and_host_safety():
    seen = []

    def respond(request):
        seen.append(str(request.url))
        assert request.url.host == "protect.gost.ru"
        if request.url.path == "/gost":
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                text=f'<a href="/gost/details/{OFFICIAL_ID}">ГОСТ 29322-92</a>'
                     f'<a href="/gost/details/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa">ГОСТ 29322-2014</a>')
        assert request.url.path == f"/gost/details/{OFFICIAL_ID}"
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text=card())

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        service = RosstandartClient(http_client=client)
        found = service.lookup("ГОСТ 29322—92")
        assert found.status == "obsolete"
        assert found.source_url == CARD_URL
        assert len(seen) == 2
        assert "search=" in seen[0]
        direct = service.lookup("ГОСТ 29322-92", source_url=CARD_URL)
        assert direct.status == "obsolete"
        assert len(seen) == 3

        ignored = service.lookup("ГОСТ 29322-92", source_url="https://evil.invalid/private")
        assert ignored.status == "obsolete"
        assert all("evil.invalid" not in item for item in seen)

    assert detail_path(CARD_URL) == f"/gost/details/{OFFICIAL_ID}"
    assert detail_path("http://protect.gost.ru/gost/details/" + OFFICIAL_ID) is None
    assert detail_path("https://protect.gost.ru.evil.invalid/gost/details/" + OFFICIAL_ID) is None
    assert parse_search(f'<a href="/gost/details/{OFFICIAL_ID}">ГОСТ 29322-2014</a>', "ГОСТ 29322-92") is None


def test_unknown_status_or_wrong_card_never_claims_obsolete():
    def respond(request):
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text=card(code="ГОСТ 29322-2014"))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        item = RosstandartClient(http_client=client).lookup("ГОСТ 29322-92", source_url=CARD_URL)
        assert item.status == "unknown"
        assert item.reason == "official_verification_unavailable"

    def failed(request):
        return httpx.Response(503, headers={"content-type": "text/html"}, text="offline")
    with httpx.Client(transport=httpx.MockTransport(failed)) as client:
        item = RosstandartClient(http_client=client).lookup("ГОСТ 29322-92")
        assert item.status == "unknown"
        assert not item.source_url

    assert RosstandartClient().lookup("IEC 61000-4-30:2008").status == "unknown"
    assert RosstandartClient().lookup("ГОСТ 29322").reason == "unsupported_or_incomplete_designation"


def make_document(client, monkeypatch):
    monkeypatch.setattr(
        "app.documents.router.extract_blocks",
        lambda data, suffix: [
            ExtractedBlock(4, 0, "2 Нормативные ссылки\nГОСТ 29322—92 Стандартные напряжения"),
            ExtractedBlock(5, 0, "Согласно ГОСТ 30804.4.30—2013 измеряют параметры"),
            ExtractedBlock(19, 0, "Библиография IEC 61000-4-30:2008"),
        ],
    )
    project = client.post("/api/v1/projects", json={"name": "Test official verification"}).json()
    uploaded = client.post(f"/api/v1/projects/{project['id']}/documents",
        data={"kind": "other"}, files={"file": ("standard.pdf", BytesIO(b"fake pdf"), "application/pdf")})
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    fragments = client.get(f"/api/v1/documents/{document_id}/fragments").json()
    for code, page, quote, role in [
        ("ГОСТ 29322-92", 4, "ГОСТ 29322—92", "normative"),
        ("ГОСТ 30804.4.30-2013", 5, "ГОСТ 30804.4.30—2013", "normative"),
        ("IEC 61000-4-30:2008", 19, "IEC 61000-4-30:2008", "informative"),
    ]:
        frag = next(f for f in fragments if f["page"] == page)
        saved = client.post(f"/api/v1/documents/{document_id}/facts", json={
            "fact_type": "normative_reference",
            "value": {"code": code, "reference_kind": role},
            "evidence": [{"fragment_id": frag["id"], "quote": quote}],
        })
        assert saved.status_code == 201, saved.text
    return project["id"], document_id


def test_report_registry_persistence_findings_and_idempotency(client, monkeypatch):
    project_id, document_id = make_document(client, monkeypatch)
    calls = []

    def fake_lookup(self, code, source_url=None):
        calls.append((code, source_url))
        if code == "ГОСТ 29322-92":
            return RegistryResult(
                code=code, status="obsolete", registry_status="Заменен",
                title="Стандартные напряжения", replacement="ГОСТ 29322-2014",
                source_url=CARD_URL, checked_at=datetime.now(timezone.utc))
        if code == "ГОСТ 30804.4.30-2013":
            return RegistryResult(
                code=code, status="active", registry_status="Действует",
                title="Методы измерения", source_url=(
                    "https://protect.gost.ru/gost/details/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                checked_at=datetime.now(timezone.utc))
        return RegistryResult(code=code, reason="unsupported_or_incomplete_designation")

    monkeypatch.setattr("app.normatives.registry.RosstandartClient.lookup", fake_lookup)
    url = f"/api/v1/documents/{document_id}/check-normatives"
    response = client.post(url)
    assert response.status_code == 200, response.text
    report = response.json()
    assert (report["total"], report["active"], report["obsolete"], report["unknown"]) == (3, 1, 1, 1)
    assert report["findings_created"] == 1
    reference = next(x for x in report["checked"] if x["code"] == "ГОСТ 29322-92")
    assert reference["source_url"] == CARD_URL
    assert reference["replacement"] == "ГОСТ 29322-2014"
    assert reference["pages"] == [4]
    assert reference["source"] == "rosstandart"
    assert reference["registry_status"] == "Заменен"
    assert reference["checked_at"] is not None
    uncertain = next(x for x in report["checked"] if x["code"].startswith("IEC"))
    assert uncertain["status"] == "unknown"
    assert uncertain["checked_at"] is None

    findings = client.get(f"/api/v1/projects/{project_id}/findings").json()
    assert len(findings) == 1
    assert findings[0]["type"] == "obsolete_normative"
    assert findings[0]["severity"] == "warning"
    assert findings[0]["evidence"][0]["quote"] == "ГОСТ 29322—92"
    assert findings[0]["evidence"][0]["page"] == 4
    assert CARD_URL in findings[0]["explanation"]
    assert "официальный статус (Росстандарт)" in findings[0]["explanation"]

    registry = client.get("/api/v1/normatives").json()
    assert {item["code"] for item in registry} == {
        "ГОСТ 29322-92", "ГОСТ 30804.4.30-2013"}
    assert next(x for x in registry if x["code"] == "ГОСТ 29322-92")["replacement"] == "ГОСТ 29322-2014"

    calls.clear()
    again = client.post(url)
    assert again.status_code == 200, again.text
    # local-first: confirmed cards need no network call; unknown IEC still does.
    assert [code for code, _ in calls] == ["IEC 61000-4-30:2008"]
    assert again.json()["findings_created"] == 0
    assert again.json()["findings_already_existing"] == 1
    again_reference = next(
        x for x in again.json()["checked"] if x["code"] == "ГОСТ 29322-92"
    )
    assert again_reference["source"] == "local"
    assert again_reference["reason"] == "local_registry_hit"
    assert again_reference["status"] == "obsolete"
    # SQLite drops tzinfo; the stored confirmation instant must not change.
    assert again_reference["checked_at"].startswith(reference["checked_at"][:19])
    assert len(client.get(f"/api/v1/projects/{project_id}/findings").json()) == 1
    assert client.post("/api/v1/documents/unknown/check-normatives").status_code == 404

    approved = client.patch(f"/api/v1/findings/{findings[0]['id']}/review",
                           json={"review_status": "accepted"})
    assert approved.status_code == 200
    assert client.post(f"/api/v1/projects/{project_id}/approve").status_code == 200
    assert client.post(url).status_code == 409


def test_local_registry_is_used_during_outage(client, monkeypatch):
    project_id, document_id = make_document(client, monkeypatch)
    manual = client.post("/api/v1/normatives", json={
        "code": "ГОСТ 29322-92", "title": "Старое название",
        "status": "obsolete", "replacement": "ГОСТ 29322-2014",
        "source_url": CARD_URL,
    })
    assert manual.status_code == 201

    calls = []

    def unknown(self, code, source_url=None):
        calls.append(code)
        return RegistryResult(code=code, reason="official_verification_unavailable")

    monkeypatch.setattr("app.normatives.registry.RosstandartClient.lookup", unknown)
    url = f"/api/v1/documents/{document_id}/check-normatives"
    response = client.post(url)
    assert response.status_code == 200, response.text
    report = response.json()
    assert (report["total"], report["active"], report["obsolete"], report["unknown"]) == (3, 0, 1, 2)
    assert "ГОСТ 29322-92" not in calls  # cached card is used without a request

    cached = next(x for x in report["checked"] if x["code"] == "ГОСТ 29322-92")
    assert cached["status"] == "obsolete"
    assert cached["source"] == "local"
    assert cached["reason"] == "local_registry_hit"
    assert cached["source_url"] == CARD_URL
    assert cached["checked_at"] is None  # manual entry has no confirmation date

    assert report["findings_created"] == 1
    findings = client.get(f"/api/v1/projects/{project_id}/findings").json()
    assert len(findings) == 1
    assert "статус по локальному реестру" in findings[0]["explanation"]
    assert "Дата подтверждения не указана" in findings[0]["explanation"]

    # A forced refresh still asks the official site, but keeps the archived
    # status when the site is unavailable.
    calls.clear()
    refreshed = client.post(f"{url}?refresh=true")
    assert refreshed.status_code == 200, refreshed.text
    assert "ГОСТ 29322-92" in calls
    item = next(
        x for x in refreshed.json()["checked"] if x["code"] == "ГОСТ 29322-92"
    )
    assert item["status"] == "obsolete"
    assert item["source"] == "local"
    assert item["reason"] == "refresh_failed_local_fallback"
    assert item["checked_at"] is None
    assert refreshed.json()["findings_already_existing"] == 1


def test_forced_refresh_updates_local_registry(client, monkeypatch):
    project_id, document_id = make_document(client, monkeypatch)
    manual = client.post("/api/v1/normatives", json={
        "code": "ГОСТ 29322-92", "title": "Старое название",
        "status": "active", "replacement": None, "source_url": CARD_URL,
    })
    assert manual.status_code == 201

    calls = []

    def lookup(self, code, source_url=None):
        calls.append((code, source_url))
        if code == "ГОСТ 29322-92":
            return RegistryResult(
                code=code, status="obsolete", registry_status="Заменен",
                title="Стандартные напряжения", replacement="ГОСТ 29322-2014",
                source_url=CARD_URL, checked_at=datetime.now(timezone.utc),
            )
        return RegistryResult(code=code, reason="unsupported_or_incomplete_designation")

    monkeypatch.setattr("app.normatives.registry.RosstandartClient.lookup", lookup)
    response = client.post(
        f"/api/v1/documents/{document_id}/check-normatives?refresh=true"
    )
    assert response.status_code == 200, response.text
    item = next(
        x for x in response.json()["checked"] if x["code"] == "ГОСТ 29322-92"
    )
    assert item["status"] == "obsolete"
    assert item["source"] == "rosstandart"
    assert item["registry_status"] == "Заменен"
    assert item["checked_at"] is not None
    assert ("ГОСТ 29322-92", CARD_URL) in calls

    registry = client.get("/api/v1/normatives", params={"search": "29322"}).json()
    updated = next(x for x in registry if x["code"] == "ГОСТ 29322-92")
    assert updated["status"] == "obsolete"
    assert updated["registry_status"] == "Заменен"
    assert updated["checked_at"] is not None
    assert updated["source_url"] == CARD_URL



def test_cancelled_standard_without_replacement_has_specific_guidance(client, monkeypatch):
    project_id, doc_id = make_document(client, monkeypatch)

    def lookup(self, code, source_url=None):
        if code == "ГОСТ 29322-92":
            return RegistryResult(
                code=code, status="obsolete", registry_status="Отменен",
                replacement=None, title="Стандартные напряжения",
                source_url=CARD_URL, checked_at=datetime.now(timezone.utc))
        return RegistryResult(code=code, reason="unsupported_or_incomplete_designation")

    monkeypatch.setattr("app.normatives.registry.RosstandartClient.lookup", lookup)
    response = client.post(f"/api/v1/documents/{doc_id}/check-normatives")
    assert response.status_code == 200, response.text
    findings = client.get(f"/api/v1/projects/{project_id}/findings").json()
    assert len(findings) == 1
    guidance = findings[0]["suggested_action"]
    assert "замена не указана" in guidance
    assert "Определите действующий нормативный документ" in guidance
    assert "применимость заменяющего стандарта" not in guidance
