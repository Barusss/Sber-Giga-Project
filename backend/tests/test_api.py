def test_project_and_report_lifecycle(client):
    assert client.get("/health").json() == {"status": "ok"}
    project = client.post("/api/v1/projects", json={"name": "Тест"})
    assert project.status_code == 201
    pid = project.json()["id"]
    response = client.post(f"/api/v1/projects/{pid}/findings", json={"module": "normatives", "type": "outdated_standard", "explanation": "Устаревший ГОСТ", "evidence": []})
    assert response.status_code == 201
    fid = response.json()["id"]
    assert client.post(f"/api/v1/projects/{pid}/approve").status_code == 409
    assert client.patch(f"/api/v1/findings/{fid}/review", json={"review_status": "accepted"}).status_code == 200
    # A reviewed manual finding is insufficient without a checked document.
    assert client.post(f"/api/v1/projects/{pid}/approve").status_code == 409
    from io import BytesIO
    from docx import Document
    doc = Document()
    doc.add_paragraph("Документ без нормативных ссылок.")
    data = BytesIO()
    doc.save(data)
    uploaded = client.post(f"/api/v1/projects/{pid}/documents", data={"kind": "other"},
        files={"file": ("notes.docx", data.getvalue(), "application/octet-stream")}).json()
    assert client.post(f"/api/v1/documents/{uploaded['id']}/extract-facts").status_code == 200
    assert client.post(f"/api/v1/projects/{pid}/approve").json()["state"] == "approved"
    assert len(client.get(f"/api/v1/projects/{pid}/report").json()["findings"]) == 1
    assert client.post(f"/api/v1/projects/{pid}/findings", json={"module": "normatives", "type": "foo", "explanation": "foo"}).status_code == 409

def test_normatives_and_unimplemented_analysis(client):
    normative = client.post("/api/v1/normatives", json={"code":"ГОСТ Р 1", "title":"Тест", "status":"active"})
    assert normative.status_code == 201
    assert client.get("/api/v1/normatives/%D0%93%D0%9E%D0%A1%D0%A2%20%D0%A0%201").status_code == 200
    assert client.post("/api/v1/requirements/analyze").status_code == 422