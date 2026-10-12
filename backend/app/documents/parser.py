"""Extract ordered, traceable text blocks from PDFs and DOCX files.

Physical DOCX page numbers cannot be known without rendering: page=1 is a
logical placeholder, and its fragment type/section is preserved instead.
"""
from dataclasses import dataclass
from io import BytesIO

import fitz
from docx import Document as WordDocument
from docx.table import Table
from docx.text.paragraph import Paragraph


class UnsupportedFile(ValueError):
    pass


@dataclass(frozen=True)
class ExtractedBlock:
    page: int
    position: int
    text: str
    block_type: str = "paragraph"
    section_path: str | None = None
    bbox: list[float] | None = None
    extraction_method: str = "text"


def extract_blocks(data: bytes, extension: str) -> list[ExtractedBlock]:
    blocks: list[ExtractedBlock] = []
    if extension == ".pdf":
        with fitz.open(stream=data, filetype="pdf") as document:
            for page_idx, page in enumerate(document, start=1):
                native = [
                    (entry[4].strip(), [round(float(coord), 2) for coord in entry[:4]])
                    for entry in page.get_text("blocks", sort=True)
                    if len(entry) > 6 and entry[6] == 0 and entry[4].strip()
                ]
                # Image-only pages (or just a scanned page number) need OCR.
                if sum(len(text) for text, _ in native) < 30:
                    import pytesseract
                    from PIL import Image
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    picture = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    content = pytesseract.image_to_string(picture, lang="rus+eng").strip()
                    native = [(content, None)] if content else []
                    method = "ocr"
                else:
                    method = "text"
                for pos, (content, bbox) in enumerate(native):
                    blocks.append(ExtractedBlock(page_idx, pos, content, bbox=bbox, extraction_method=method))
        return blocks

    if extension == ".docx":
        doc = WordDocument(BytesIO(data))
        section = None
        for element in doc.iter_inner_content():
            if isinstance(element, Paragraph):
                content = element.text.strip()
                if not content:
                    continue
                style = (element.style.name or "").lower() if element.style else ""
                is_heading = style.startswith("heading") or style.startswith("заголовок")
                if is_heading:
                    section = content
                blocks.append(ExtractedBlock(1, len(blocks), content, "heading" if is_heading else "paragraph", section))
            elif isinstance(element, Table):
                for row in element.rows:
                    text = " | ".join(cell.text.strip() for cell in row.cells)
                    if text.strip(" |"):
                        blocks.append(ExtractedBlock(1, len(blocks), text, "table_row", section))
        return blocks

    raise UnsupportedFile("Supported formats: PDF, DOCX")


def extract_pages(data: bytes, extension: str) -> list[str]:
    """Keep the original page-text helper for callers that still use it."""
    blocks = extract_blocks(data, extension)
    pages: dict[int, list[str]] = {}
    for block in blocks:
        pages.setdefault(block.page, []).append(block.text)
    return ["\n\n".join(pages[p]) for p in sorted(pages)]
