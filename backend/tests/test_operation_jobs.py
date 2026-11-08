"""Operations must persist in the DB and outlive the request that starts them."""
from io import BytesIO
from threading import Event, Lock
from types import SimpleNamespace
import time

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import pytest

from app.core.db import Base, get_db
from app.main import app
from app.documents.parser import ExtractedBlock


@pytest.fixture
def ops_client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'jobs.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("app.operations.SessionLocal", Session)
    from app.core.config import settings
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")

    def override():
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def wait_for(client, url, wanted="completed"):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        response = client.get(url)
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in ("completed", "failed", "cancelled"):
            assert job["status"] == wanted, job
            return job
        time.sleep(.035)
    pytest.fail("Background job did not finish")


def create_project(client):
    response = client.post("/api/v1/projects", json={"name": "Проверка восстановления"})
    assert response.status_code == 201
    return response.json()["id"]


def create_document(client, project_id, kind, monkeypatch):
    monkeypatch.setattr(
        "app.documents.router.extract_blocks",
        lambda data, suffix: [ExtractedBlock(1, 0, "1.1. Установить мощность 150 кВт")],
    )
    response = client.post(
        f"/api/v1/projects/{project_id}/documents",
        data={"kind": kind},
        files={"file": (f"{kind}.pdf", BytesIO(b"valid pdf mock"), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_upload_survives_client_refresh_and_reports_true_stages(ops_client, monkeypatch):
    project_id = create_project(ops_client)

    def slow_ocr(data, suffix):
        time.sleep(.15)
        return [ExtractedBlock(1, 0, "1.1. Установить максимальную мощность 150 кВт")]

    monkeypatch.setattr("app.operations.extract_blocks", slow_ocr)
    response = ops_client.post(
        f"/api/v1/projects/{project_id}/operations/upload",
        data={"kind": "tu"},
        files={"file": ("tu.pdf", BytesIO(b"pdf data"), "application/pdf")},
    )
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status"] in ("queued", "running")
    assert job["kind"] == "upload"

    # A new browser request recovers the same operation id, not a fresh job.
    with TestClient(app) as after_reload:
        listed = after_reload.get(f"/api/v1/projects/{project_id}/operations")
        assert listed.status_code == 200
        assert listed.json()[0]["id"] == job["id"]
        finished = wait_for(
            after_reload,
            f"/api/v1/projects/{project_id}/operations/{job['id']}",
        )
    assert finished["result"]["document_id"]
    assert finished["completed_at"] is not None
    assert len(ops_client.get(f"/api/v1/projects/{project_id}/documents").json()) == 1


def test_failed_upload_stays_failed_after_refresh(ops_client, monkeypatch):
    project_id = create_project(ops_client)

    def broken(data, suffix):
        raise ValueError("test parser exception")

    monkeypatch.setattr("app.operations.extract_blocks", broken)
    response = ops_client.post(
        f"/api/v1/projects/{project_id}/operations/upload",
        data={"kind": "znp"},
        files={"file": ("bad.pdf", BytesIO(b"bad file"), "application/pdf")},
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    job = wait_for(ops_client,
                   f"/api/v1/projects/{project_id}/operations/{job_id}",
                   wanted="failed")
    assert "test parser exception" in job["error"]
    assert job["progress_current"] is None
    assert not ops_client.get(f"/api/v1/projects/{project_id}/documents").json()
    assert ops_client.get(f"/api/v1/projects/{project_id}/operations").json()[0]["status"] == "failed"


def test_batch_comparisons_complete_after_request_returns(ops_client, monkeypatch):
    project_id = create_project(ops_client)
    tu = create_document(ops_client, project_id, "tu", monkeypatch)
    znp = create_document(ops_client, project_id, "znp", monkeypatch)
    second_znp = create_document(ops_client, project_id, "znp", monkeypatch)
    create = ops_client.post(
        f"/api/v1/projects/{project_id}/comparisons", json={"pairs": [
            {"tu_document_id": tu, "znp_document_id": znp},
            {"tu_document_id": tu, "znp_document_id": second_znp},
        ]},
    )
    assert create.status_code == 201, create.text
    pair_ids = [p["id"] for p in create.json()]

    def mock_run(pid, pair_id, db):
        time.sleep(.09)
        return {"run": {"id": "saved-" + pair_id}}

    monkeypatch.setattr("app.operations.execute_comparison", mock_run)
    start = ops_client.post(
        f"/api/v1/projects/{project_id}/operations",
        json={"kind": "comparison_batch", "pair_ids": pair_ids},
    )
    assert start.status_code == 202, start.text
    job_id = start.json()["id"]
    assert start.json()["progress_current"] is None

    duplicate = ops_client.post(
        f"/api/v1/projects/{project_id}/operations",
        json={"kind": "comparison", "pair_ids": pair_ids[:1]},
    )
    assert duplicate.status_code == 409
    finished = wait_for(ops_client, f"/api/v1/projects/{project_id}/operations/{job_id}")
    assert finished["progress_current"] == finished["progress_total"] == 2
    assert [item["pair_id"] for item in finished["result"]["runs"]] == pair_ids
    assert len(ops_client.get(f"/api/v1/projects/{project_id}/operations").json()) == 1


def test_operation_rejects_foreign_project_documents(ops_client, monkeypatch):
    first = create_project(ops_client)
    second = create_project(ops_client)
    document = create_document(ops_client, first, "tu", monkeypatch)
    response = ops_client.post(
        f"/api/v1/projects/{second}/operations",
        json={"kind": "normative_extract", "document_id": document},
    )
    assert response.status_code == 422
    assert ops_client.get(f"/api/v1/projects/{second}/operations").json() == []


def mock_normative_batch(doc, offset, total=3):
    record = SimpleNamespace(model_dump=lambda mode=None: {
        "code": f"TEST-{offset}", "reference_kind": "normative",
        "status": "unknown", "registry_status": None, "replacement": None,
        "source_url": None, "checked_at": None, "pages": [],
        "reason": "unavailable", "source": "unknown",
    })
    return SimpleNamespace(
        document_id=doc.id, total=1, active=0, obsolete=0, unknown=1,
        findings_created=0, findings_already_existing=0, checked=[record],
        document_total=total, offset=offset,
        next_offset=offset + 1 if offset + 1 < total else None,
    )


def test_normative_check_stores_batch_progress_and_final_report(ops_client, monkeypatch):
    project_id = create_project(ops_client)
    doc_id = create_document(ops_client, project_id, "tu", monkeypatch)

    def fake_check(db, document, offset, limit, refresh):
        time.sleep(.055)
        return mock_normative_batch(document, offset)

    monkeypatch.setattr("app.operations.check_document_standards", fake_check)
    response = ops_client.post(
        f"/api/v1/projects/{project_id}/operations",
        json={"kind": "normative_check", "document_id": doc_id},
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    completed = wait_for(
        ops_client, f"/api/v1/projects/{project_id}/operations/{job_id}",
    )
    assert completed["progress_total"] == 3
    assert completed["progress_current"] == 3
    assert completed["result"]["total"] == 3
    assert len(completed["result"]["checked"]) == 3


def test_normative_check_can_stop_after_current_batch(ops_client, monkeypatch):
    project_id = create_project(ops_client)
    doc_id = create_document(ops_client, project_id, "tu", monkeypatch)
    started = Event()

    def fake_check(db, document, offset, limit, refresh):
        started.set()
        time.sleep(.2)
        return mock_normative_batch(document, offset)

    monkeypatch.setattr("app.operations.check_document_standards", fake_check)
    response = ops_client.post(
        f"/api/v1/projects/{project_id}/operations",
        json={"kind": "normative_check", "document_id": doc_id},
    )
    assert response.status_code == 202
    job_id = response.json()["id"]
    assert started.wait(4)
    stopped = ops_client.post(
        f"/api/v1/projects/{project_id}/operations/{job_id}/cancel",
    )
    assert stopped.status_code == 200
    job = wait_for(
        ops_client, f"/api/v1/projects/{project_id}/operations/{job_id}",
        wanted="cancelled",
    )
    assert job["progress_current"] == 1
    assert job["progress_total"] == 3
    assert job["result"]["total"] == 1


def test_background_worker_queue_does_not_exhaust_db_pool(ops_client, monkeypatch):
    """The third CPU/network-heavy operation waits without acquiring a DB connection."""
    jobs = []
    started = []
    lock = Lock()
    two_started = Event()
    release = Event()

    def slow_check(db, document, offset, limit, refresh):
        with lock:
            started.append(document.id)
            if len(started) == 2:
                two_started.set()
        if not release.wait(5):
            raise AssertionError("Test worker was never released")
        return mock_normative_batch(document, offset, total=1)

    monkeypatch.setattr("app.operations.check_document_standards", slow_check)
    try:
        for _ in range(3):
            project_id = create_project(ops_client)
            document_id = create_document(ops_client, project_id, "tu", monkeypatch)
            response = ops_client.post(
                f"/api/v1/projects/{project_id}/operations",
                json={"kind": "normative_check", "document_id": document_id},
            )
            assert response.status_code == 202, response.text
            jobs.append((project_id, response.json()["id"]))

        assert two_started.wait(4), "Two worker slots were not started"
        third_project, third_id = jobs[2]
        third_status = ops_client.get(
            f"/api/v1/projects/{third_project}/operations/{third_id}"
        )
        assert third_status.status_code == 200, third_status.text
        assert third_status.json()["status"] == "queued"
        assert len(started) == 2
    finally:
        release.set()

    for project_id, job_id in jobs:
        finished = wait_for(
            ops_client, f"/api/v1/projects/{project_id}/operations/{job_id}"
        )
        assert finished["status"] == "completed"
