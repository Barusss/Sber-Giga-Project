"""Synthetic scanned PDFs exercise OCR layout reconstruction without Tesseract binaries.

The OCR recognizer is mocked, but the image-only PDF branch, page/coordinate
provenance, section-aware chunking, and requirement extraction run end-to-end.
"""
from io import BytesIO

import fitz
import pytesseract
from PIL import Image

from app.documents.chunking import build_chunks
from app.documents.parser import extract_blocks
from app.requirements.extraction import extract_clauses


def pdf_with_blank_pages(count=2):
    document = fitz.open()
    for _ in range(count):
        document.new_page(width=595, height=842)
    result = document.tobytes()
    document.close()
    return result


def fake_ocr_rows(lines):
    values = {key: [] for key in (
        "text", "left", "top", "width", "height",
        "block_num", "par_num", "line_num",
    )}
    for row_index, line in enumerate(lines):
        x = 35
        for word in line.split():
            width = max(8, len(word) * 8)
            for name, value in (
                ("text", word), ("left", x), ("top", 30 + 32 * row_index),
                ("width", width), ("height", 22),
                ("block_num", 1), ("par_num", row_index + 1), ("line_num", 1),
            ):
                values[name].append(value)
            x += width + 8
    return values


def test_scanned_pdf_sections_citations_and_numbered_requirements(monkeypatch):
    first = [
        "СОДЕРЖАНИЕ",
        "1. Общие сведения ..... 2",
        "2. Электротехнические решения ..... 2",
    ]
    second = [
        "1. ОБЩИЕ СВЕДЕНИЯ",
        "1.1. Технические параметры",
        "1.1.1. Установить мощность 236 МВт.",
        "2. ЭЛЕКТРОТЕХНИЧЕСКИЕ РЕШЕНИЯ",
        "2.1. Схема присоединения",
        "2.1.1. Предусмотреть напряжение 110 кВ.",
        "Стр. 2 из 2",
    ]
    calls = []

    def fake_data(picture, **kwargs):
        calls.append((picture.width, picture.height))
        return fake_ocr_rows([first, second][len(calls) - 1])

    monkeypatch.setattr(pytesseract, "image_to_data", fake_data)
    blocks = extract_blocks(pdf_with_blank_pages(), ".pdf")

    assert len(calls) == 2
    assert all(block.extraction_method == "ocr" for block in blocks)
    assert len([b for b in blocks if b.block_type == "toc"]) == len(first)
    assert all(b.page == 1 for b in blocks[:len(first)])
    assert all(b.page == 2 for b in blocks[len(first):])
    assert all(b.bbox and b.bbox[2] > b.bbox[0] for b in blocks)
    requirement = next(b for b in blocks if "мощность 236 МВт" in b.text)
    assert requirement.block_type == "paragraph"
    assert requirement.section_path == (
        "1. ОБЩИЕ СВЕДЕНИЯ / 1.1. Технические параметры"
    )
    second_requirement = next(b for b in blocks if "напряжение 110 кВ" in b.text)
    assert second_requirement.section_path == (
        "2. ЭЛЕКТРОТЕХНИЧЕСКИЕ РЕШЕНИЯ / 2.1. Схема присоединения"
    )
    assert blocks[-1].block_type == "page_furniture"

    chunks = build_chunks(blocks)
    assert len(chunks) >= 2
    assert not any("СОДЕРЖАНИЕ" in chunk.text for chunk in chunks)
    assert not any("Стр. 2 из 2" in chunk.text for chunk in chunks)
    assert all(chunk.section_path for chunk in chunks)
    assert any("236 МВт" in chunk.text for chunk in chunks)
    assert any("110 кВ" in chunk.text for chunk in chunks)
    for chunk in chunks:
        for piece in chunk.pieces:
            assert piece.fragment.text[piece.start:piece.end] in chunk.text

    for i, block in enumerate(blocks):
        object.__setattr__(block, "id", f"fragment-{i}")
    clauses = extract_clauses(blocks)
    assert not any("СОДЕРЖАНИЕ" in item.text for item in clauses)
    assert not any("Стр. 2 из 2" in item.text for item in clauses)
    assert any("236 МВт" in item.text for item in clauses)


def test_scanned_page_with_partial_native_text_overlay_uses_ocr(monkeypatch):
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    picture = Image.new("RGB", (50, 50), "white")
    buffer = BytesIO()
    picture.save(buffer, format="PNG")
    page.insert_image(page.rect, stream=buffer.getvalue())
    page.insert_text((35, 40), "Native text layer containing only a few words")
    pdf = document.tobytes()
    document.close()

    calls = []

    def fake_data(picture, **kwargs):
        calls.append(1)
        return fake_ocr_rows(["1. ОБЩИЕ СВЕДЕНИЯ", "1.1. Мощность", "Мощность 236 МВт."])

    monkeypatch.setattr(pytesseract, "image_to_data", fake_data)
    blocks = extract_blocks(pdf, ".pdf")
    assert calls == [1]
    assert blocks[0].extraction_method == "ocr"
    assert any("236 МВт" in block.text for block in blocks)


def test_ocr_line_wrapping_and_separate_requirements(monkeypatch):
    def fake_data(picture, **kwargs):
        values = fake_ocr_rows([
            "1. ОБЩИЕ СВЕДЕНИЯ", "1.1. Условия подключения",
            "1.1.1. Выполнить подключение", "к электрическим сетям 110 кВ.",
            "1.1.2. Обеспечить два независимых канала.",
        ])
        # Simulate two visual lines belonging to one Tesseract paragraph.
        values["par_num"][sum(len(x.split()) for x in [
            "1. ОБЩИЕ СВЕДЕНИЯ", "1.1. Условия подключения",
            "1.1.1. Выполнить подключение",
        ]):sum(len(x.split()) for x in [
            "1. ОБЩИЕ СВЕДЕНИЯ", "1.1. Условия подключения",
            "1.1.1. Выполнить подключение", "к электрическим сетям 110 кВ.",
        ])] = [3] * len("к электрическим сетям 110 кВ.".split())
        return values

    monkeypatch.setattr(pytesseract, "image_to_data", fake_data)
    blocks = extract_blocks(pdf_with_blank_pages(1), ".pdf")
    requirement = next(b for b in blocks if "Выполнить подключение" in b.text)
    assert "110 кВ" in requirement.text
    assert any("Обеспечить два независимых" in b.text for b in blocks)
    assert len([b for b in blocks if b.block_type == "heading"]) == 2
