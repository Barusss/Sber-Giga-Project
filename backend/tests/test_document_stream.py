"""The streaming diagnostic upload must preserve the normal document contract."""
import json

from app.documents.parser import ExtractedBlock


def _events(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_streamed_upload_reports_real_stages_and_saves(client, monkeypatch, tmp_path):
    monkeypatch.setattr("app.documents.router.settings.upload_dir", tmp_path)
    monkeypatch.setattr(
        "app.documents.router.extract_blocks",
        lambda data, suffix: [
            ExtractedBlock(1, 0, "ГОСТ 29322-2014 и технические требования"),
            ExtractedBlock(2, 0, "Обязательные требования технических условий"),
        ],
    )
    project = client.post("/api/v1/projects", json={"name": "OCR test"}).json()
    url = f"/api/v1/projects/{project['id']}/documents/stream"
    response = client.post(
        url, data={"kind": "tu"},
        files={"file": ("scan.pdf", b"fixture", "application/pdf")},
    )

    events = _events(response)
    assert [event["stage"] for event in events] == [
        "received", "parsing", "parsed", "indexing", "complete"
    ]
    assert events[2]["pages"] == 2
    assert events[2]["fragments"] == 2
    document = events[-1]["document"]
    assert document["filename"] == "scan.pdf"
    assert document["project_id"] == project["id"]
    fragments = client.get(f"/api/v1/documents/{document['id']}/fragments").json()
    assert len(fragments) == 2
    assert {fragment["page"] for fragment in fragments} == {1, 2}


def test_streamed_upload_reports_ocr_failure_without_saving(client, monkeypatch):
    project = client.post("/api/v1/projects", json={"name": "Broken scan"}).json()

    def fail_parsing(data, extension):
        raise RuntimeError("Private internal error must not leak")

    monkeypatch.setattr("app.documents.router.extract_blocks", fail_parsing)
    response = client.post(
        f"/api/v1/projects/{project['id']}/documents/stream",
        data={"kind": "other"},
        files={"file": ("broken.pdf", b"fixture", "application/pdf")},
    )
    events = _events(response)
    assert [event["stage"] for event in events] == ["received", "parsing", "error"]
    assert events[-1]["step"] == "parsing"
    assert "Private internal" not in response.text
    assert client.get(f"/api/v1/projects/{project['id']}/documents").json() == []


def test_streamed_upload_keeps_validation_http_errors(client):
    project = client.post("/api/v1/projects", json={"name": "Validation"}).json()
    response = client.post(
        f"/api/v1/projects/{project['id']}/documents/stream",
        data={"kind": "tu"},
        files={"file": ("bad.txt", b"something", "text/plain")},
    )
    assert response.status_code == 415


def test_regular_upload_still_returns_201(client, monkeypatch, tmp_path):
    monkeypatch.setattr("app.documents.router.settings.upload_dir", tmp_path)
    monkeypatch.setattr(
        "app.documents.router.extract_blocks",
        lambda data, suffix: [ExtractedBlock(1, 0, "Текст документа")],
    )
    project = client.post("/api/v1/projects", json={"name": "Old API"}).json()
    response = client.post(
        f"/api/v1/projects/{project['id']}/documents",
        data={"kind": "znp"},
        files={"file": ("native.pdf", b"fixture", "application/pdf")},
    )
    assert response.status_code == 201
    assert response.json()["filename"] == "native.pdf"
