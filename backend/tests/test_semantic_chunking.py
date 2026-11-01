from io import BytesIO
from docx import Document
from app.documents.parser import extract_blocks
from app.documents.chunking import build_chunks


def test_unstyled_headings_and_contents():
    doc = Document()
    for line in ("СОДЕРЖАНИЕ", "1. Общие сведения", "1. ОБЩИЕ СВЕДЕНИЯ",
                 "1.1. Назначение", "Проектирование выполняется на 110 кВ.",
                 "1.2. Оборудование", "Применяется трансформатор 250 МВА."):
        doc.add_paragraph(line)
    buf = BytesIO()
    doc.save(buf)
    blocks = extract_blocks(buf.getvalue(), ".docx")
    chunks = build_chunks(blocks)
    # An ambiguous undotted entry is retained rather than dropping body text.
    assert sum(b.block_type == "toc" for b in blocks) == 1
    assert "1.1. Назначение" in blocks[4].section_path
    assert len(chunks) >= 2
    assert all(chunk.section_path for chunk in chunks)
    assert not any("СОДЕРЖАНИЕ" in chunk.text for chunk in chunks)


def test_exact_source_span_coverage():
    doc = Document()
    doc.add_paragraph("1. ЗАГОЛОВОК")
    doc.add_paragraph("1.1. Параметры")
    doc.add_paragraph("Мощность 236 МВт. " * 100)
    buf = BytesIO()
    doc.save(buf)
    blocks = extract_blocks(buf.getvalue(), ".docx")
    chunks = build_chunks(blocks, max_chars=220)
    assert max(len(chunk.text) for chunk in chunks) <= 220
    for block in blocks:
        spans = sorted((piece.start, piece.end) for chunk in chunks
                       for piece in chunk.pieces
                       if piece.fragment.position == block.position)
        assert spans[0][0] == 0
        assert spans[-1][1] == len(block.text)
        assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))


def test_table_rows_do_not_mix_with_prose():
    doc = Document()
    doc.add_paragraph("1. ГЛАВНЫЙ РАЗДЕЛ")
    doc.add_paragraph("1.1. Показатели")
    doc.add_paragraph("Параметры в таблице ниже.")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Параметр"
    table.cell(0, 1).text = "Значение"
    table.cell(1, 0).text = "Мощность"
    table.cell(1, 1).text = "236 МВт"
    doc.add_paragraph("После таблицы необходимо провести испытания.")
    buffer = BytesIO()
    doc.save(buffer)
    blocks = extract_blocks(buffer.getvalue(), ".docx")
    chunks = build_chunks(blocks)
    assert any(b.block_type == "table_row" for b in blocks)
    assert all(not (
        any(p.fragment.block_type == "table_row" for p in chunk.pieces)
        and any(p.fragment.block_type == "paragraph" for p in chunk.pieces)
    ) for chunk in chunks)


def test_reindex_preserves_fragment_ids_and_fact_citations(client):
    doc = Document()
    doc.add_paragraph("1. ГЛАВНЫЙ РАЗДЕЛ")
    doc.add_paragraph("1.1. Условия присоединения")
    doc.add_paragraph("Установить два независимых канала связи 110 кВ.")
    buffer = BytesIO()
    doc.save(buffer)
    project = client.post("/api/v1/projects", json={"name": "Аудит чанков"}).json()
    response = client.post(
        f"/api/v1/projects/{project['id']}/documents",
        data={"kind": "tu"},
        files={"file": ("check.docx", BytesIO(buffer.getvalue()),
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert response.status_code == 201, response.text
    doc_id = response.json()["id"]
    before = client.get(f"/api/v1/documents/{doc_id}/fragments").json()
    source = next(item for item in before if "два независимых" in item["text"])
    fact = client.post(f"/api/v1/documents/{doc_id}/facts", json={
        "fact_type": "engineering_requirement",
        "value": {"description": "два канала связи"},
        "evidence": [{"fragment_id": source["id"], "quote": "два независимых канала связи"}],
    })
    assert fact.status_code == 201, fact.text
    for _ in range(2):
        indexed = client.post(f"/api/v1/documents/{doc_id}/reindex-chunks")
        assert indexed.status_code == 200, indexed.text
        assert indexed.json()["chunking_version"] == "chunks-v3"
    after = client.get(f"/api/v1/documents/{doc_id}/fragments").json()
    assert [(item["id"], item["text"]) for item in before] == [
        (item["id"], item["text"]) for item in after
    ]
    facts = client.get(f"/api/v1/documents/{doc_id}/facts").json()
    assert any(source["id"] == quote["fragment_id"]
               for item in facts for quote in item["evidence"])
