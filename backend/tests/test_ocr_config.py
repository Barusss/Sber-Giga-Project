"""PDF OCR configuration: scanned pages use Tesseract, text PDFs do not."""
import fitz
import pytesseract

from app.documents.parser import extract_blocks


def test_scanned_pdf_uses_russian_english_lstm(monkeypatch):
    calls = {}

    def fake_ocr(image, *, lang, config, output_type):
        calls["language"] = lang
        calls["config"] = config
        calls["size"] = image.size
        return {
            "text": ["ГОСТ", "29322-2014", "Технические", "условия"],
            "left": [10, 70, 10, 120],
            "top": [20, 20, 55, 55],
            "width": [50, 110, 100, 100],
            "height": [20, 20, 20, 20],
            "block_num": [1, 1, 1, 1],
            "par_num": [1, 1, 2, 2],
            "line_num": [1, 1, 1, 1],
        }

    monkeypatch.setattr(pytesseract, "image_to_data", fake_ocr)
    with fitz.open() as pdf:
        pdf.new_page()
        pdf_bytes = pdf.tobytes()

    blocks = extract_blocks(pdf_bytes, ".pdf")

    assert len(blocks) == 2
    assert blocks[0].extraction_method == "ocr"
    assert blocks[0].text.startswith("ГОСТ 29322-2014")
    assert calls["language"] == "rus+eng"
    assert calls["config"] == "--oem 1 --psm 3"
    assert calls["size"][0] > 0


def test_text_pdf_does_not_run_ocr(monkeypatch):
    def forbidden_ocr(*args, **kwargs):
        raise AssertionError("OCR should not run on a text PDF")

    monkeypatch.setattr(pytesseract, "image_to_data", forbidden_ocr)
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "A document with a sufficiently long searchable text paragraph.")
        pdf_bytes = pdf.tobytes()

    blocks = extract_blocks(pdf_bytes, ".pdf")

    assert blocks
    assert all(block.extraction_method == "text" for block in blocks)
