"""Extract ordered, traceable text blocks from PDFs and DOCX files.

Physical DOCX page numbers cannot be known without rendering: page=1 is a
logical placeholder, and its fragment type/section is preserved instead.
"""
from dataclasses import dataclass
from io import BytesIO
import re

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



# Most engineering DOCX files use numbered headings, but some use "Normal"
# instead of Word heading styles. Detect those only if the document has no
# styled headings, avoiding confusion with numbered requirements in a TU.
_ROOT = re.compile(r"^([1-9]\d?)\.\s+(.+)$")
_SUBHEADING = re.compile(r"^([1-9]\d?(?:\.\d{1,2}){1,2})\.\s+(.+)$")
_APPENDIX = re.compile(r"^Приложение\s+[А-ЯA-Z]\s*[.\-:]\s*.+$", re.I)


def heading_level(text: str, style: str, allow_unstyled: bool = True) -> int | None:
    if style.startswith(("heading", "заголовок")):
        match = re.search(r"\d+", style)
        return min(int(match.group()) if match else 1, 3)

    if not allow_unstyled or style.startswith("list"):
        return None
    root = _ROOT.match(text)
    if root:
        letters = "".join(ch for ch in root.group(2) if ch.isalpha())
        if letters and letters == letters.upper() and len(text) <= 160:
            return 1
    sub = _SUBHEADING.match(text)
    if sub and len(text) <= 145 and not text.endswith((".", ";", "!", "?")):
        return min(sub.group(1).count(".") + 1, 3)
    if _APPENDIX.match(text) and len(text) <= 150:
        return 1
    return None


def _extract_docx(data: bytes) -> list[ExtractedBlock]:
    doc = WordDocument(BytesIO(data))
    blocks: list[ExtractedBlock] = []
    headings: dict[int, str] = {}
    has_styled_headings = any(
        isinstance(item, Paragraph) and item.style
        and (item.style.name or "").lower().startswith(("heading", "заголовок"))
        for item in doc.iter_inner_content()
    )
    in_toc = False

    for element in doc.iter_inner_content():
        if isinstance(element, Paragraph):
            content = element.text.strip()
            if not content:
                continue
            style = (element.style.name or "").lower() if element.style else ""
            level = heading_level(content, style, allow_unstyled=not has_styled_headings)

            if content.casefold() in {"содержание", "оглавление"}:
                in_toc = True
                headings.clear()
                blocks.append(ExtractedBlock(1, len(blocks), content, "toc"))
                continue

            if in_toc:
                # TOC entries must not pollute the searchable chunks.
                is_body_root = (
                    level == 1 and
                    (style.startswith(("heading", "заголовок"))
                     or bool(_ROOT.match(content) and content == content.upper()))
                )
                if not is_body_root:
                    blocks.append(ExtractedBlock(1, len(blocks), content, "toc"))
                    continue
                in_toc = False

            if level:
                headings[level] = content
                for previous in list(headings):
                    if previous > level:
                        del headings[previous]

            section = " / ".join(headings[i] for i in sorted(headings)) or None
            block_type = "heading" if level else "paragraph"
            blocks.append(ExtractedBlock(
                1, len(blocks), content, block_type, section,
            ))
        elif isinstance(element, Table):
            section = " / ".join(headings[i] for i in sorted(headings)) or None
            for row in element.rows:
                content = " | ".join(cell.text.strip() for cell in row.cells)
                if content.strip(" |"):
                    block_type = "toc" if in_toc else "table_row"
                    blocks.append(ExtractedBlock(
                        1, len(blocks), content, block_type,
                        None if in_toc else section,
                    ))
    return blocks


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
                    content = pytesseract.image_to_string(
                        picture, lang="rus+eng", config="--oem 1 --psm 3"
                    ).strip()
                    native = [(content, None)] if content else []
                    method = "ocr"
                else:
                    method = "text"
                for pos, (content, bbox) in enumerate(native):
                    blocks.append(ExtractedBlock(page_idx, pos, content, bbox=bbox, extraction_method=method))
        return blocks

    if extension == ".docx":
        return _extract_docx(data)

    raise UnsupportedFile("Supported formats: PDF, DOCX")


def extract_pages(data: bytes, extension: str) -> list[str]:
    """Keep the original page-text helper for callers that still use it."""
    blocks = extract_blocks(data, extension)
    pages: dict[int, list[str]] = {}
    for block in blocks:
        pages.setdefault(block.page, []).append(block.text)
    return ["\n\n".join(pages[p]) for p in sorted(pages)]
