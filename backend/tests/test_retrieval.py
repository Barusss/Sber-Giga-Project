from io import BytesIO
import fitz



def make_pdf(page_count=100):
    pdf = fitz.open()
    for page_no in range(1, page_count + 1):
        page = pdf.new_page()
        content = {
            1: "Maximum allowed power of the planned connection is 150 kW.",
            45: "The electrical connection point is cabinet seven at 10 kV.",
            89: "The agreed completion deadline is 24 months from signing.",
        }.get(page_no, f"Engineering appendix page {page_no}: ordinary description of equipment.")
        page.insert_text((40, 70), content)
    content = pdf.tobytes()
    pdf.close()
    return content


def test_scattered_facts_100_pages(client):
    project = client.post("/api/v1/projects", json={"name": "100 страниц"}).json()
    pid = project["id"]
    uploaded = client.post(
        f"/api/v1/projects/{pid}/documents", data={"kind": "tu"},
        files={"file": ("conditions.pdf", BytesIO(make_pdf()), "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    doc = uploaded.json()["id"]
    fragments = client.get(f"/api/v1/documents/{doc}/fragments").json()
    assert len(fragments) == 100
    assert {f["page"] for f in fragments} == set(range(1, 101))
    chunks = client.get(f"/api/v1/documents/{doc}/chunks?limit=500").json()
    assert chunks
    assert len(chunks) < 100  # short nearby fragments should combine
    for chunk in chunks:
        assert chunk["sources"]
        for source in chunk["sources"]:
            frag = next(f for f in fragments if f["id"] == source["fragment_id"])
            excerpt = frag["text"][source["start_offset"]:source["end_offset"]]
            assert excerpt in chunk["text"]
            assert source["page"] == frag["page"]

    for typ, search, page, quote in [
        ("power_limit", "Maximum allowed power", 1, "150 kW"),
        ("connection_point", "electrical connection point", 45, "cabinet seven"),
        ("completion_deadline", "completion deadline", 89, "24 months"),
    ]:
        result = client.post(f"/api/v1/documents/{doc}/search", json={"query": search, "limit": 5})
        assert result.status_code == 200, result.text
        hits = result.json()
        assert any(h["chunk"]["page_start"] <= page <= h["chunk"]["page_end"] for h in hits)
        source_fragment = next(f for f in fragments if f["page"] == page)
        fact = client.post(f"/api/v1/documents/{doc}/facts", json={
            "fact_type": typ, "value": {"value": quote}, "confidence": 0.96,
            "extraction_version": "unit-test-v1",
            "evidence": [{"fragment_id": source_fragment["id"], "quote": quote}],
        })
        assert fact.status_code == 201, fact.text
        assert fact.json()["evidence"][0]["page"] == page
        assert len(client.get(f"/api/v1/documents/{doc}/facts?fact_type={typ}").json()) == 1

    bad_quote = client.post(f"/api/v1/documents/{doc}/facts", json={
        "fact_type": "power_limit", "value": {"value": "fiction"},
        "evidence": [{"fragment_id": fragments[0]["id"], "quote": "invented text"}],
    })
    assert bad_quote.status_code == 422
    no_source = client.post(f"/api/v1/documents/{doc}/facts", json={
        "fact_type": "power_limit", "value": {"value": "fiction"}, "evidence": [],
    })
    assert no_source.status_code == 422
    assert len(client.get(f"/api/v1/documents/{doc}/facts").json()) == 3
    assert client.post(f"/api/v1/documents/{doc}/search", json={"query": "nonexistentfoobar"}).json() == []
    assert client.get("/api/v1/fact-types").json()["power_limit"]


def test_long_ocr_blocks_are_chunked_and_traced(client):
    from app.documents.chunking import build_chunks
    from app.documents.parser import ExtractedBlock

    piece = "Long engineering requirement, " * 200
    blocks = [ExtractedBlock(17, 0, piece)]
    drafts = build_chunks(blocks, max_chars=300)
    assert len(drafts) > 1
    assert "".join(p.text for p in drafts) == piece
    assert all(len(p.text) <= 300 for p in drafts)
    assert drafts[-1].page_end == 17


def test_search_validation_and_sqlite_semantic_limitation(client):
    project = client.post("/api/v1/projects", json={"name": "Проверка"}).json()
    doc = client.post(f"/api/v1/projects/{project['id']}/documents", data={"kind": "tu"}, files={
        "file": ("small.pdf", BytesIO(make_pdf(2)), "application/pdf")
    }).json()["id"]
    assert client.post(f"/api/v1/documents/{doc}/search", json={}).status_code == 422
    assert client.post(f"/api/v1/documents/{doc}/search", json={"embedding": [0.1] * 384}).status_code == 422
    assert client.post(f"/api/v1/documents/{doc}/search", json={
        "embedding": [0.1] * 384, "embedding_model": "sample-v1"
    }).status_code == 501
    assert client.get(f"/api/v1/documents/{doc}/chunks?limit=9999").status_code == 422
