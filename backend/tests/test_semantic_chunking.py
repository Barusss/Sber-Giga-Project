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
    assert sum(b.block_type == "toc" for b in blocks) == 2
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
