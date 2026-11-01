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



# Scan OCR has no Word styles. Recover only unambiguous numbered section
# headings; a numbered engineering requirement must remain ordinary prose.
_OCR_CLAUSE_VERBS = re.compile(
    r"\b(?:долж(?:ен|на|но|ны)|обязан\w*|предусматрива\w*|"
    r"обеспечива\w*|устанавлива\w*|выполня\w*|"
    r"применя\w*|производ\w*|необходим\w*|"\n    r"установить|выполнить|обеспечить|предусмотреть|разработать|"\n    r"подключить|соблюдать|следует)\b", re.I,
)
_PAGE_FOOTER = re.compile(
    r"^\s*(?:стр(?:аница)?\.?\s*)?\d{1,4}\s*(?:из|/)\s*\d{1,4}\s*$",
    re.I,
)
_NUMBERED_LINE = re.compile(
    r"^\s*\d{1,3}(?:\.\d{1,3}){0,5}[.)]?\s+"
)
_BULLET_LINE = re.compile(r"^\s*[-—–•●]\s+\S")
_CONTENTS_LINE = re.compile(r"(?:\.{3,}|…{2,})\s*\d{1,4}\s*$")


def _ocr_heading_level(text: str) -> int | None:
    """Conservative layout inference: never promote a numbered command."""
    if _OCR_CLAUSE_VERBS.search(text):
        return None
    # In scanned conditions, 1.2.3. is usually an atomic requirement,
    # not a section heading. Avoid assuming one from noisy OCR typography.
    if re.match(r"^\s*\d{1,3}(?:\.\d{1,3}){2,}[.)]?\s+", text):
        return None
    return heading_level(text, "", allow_unstyled=True)


def _ocr_lines(page, picture, page_idx: int):
    """Read OCR word coordinates once, retaining line order and real PDF boxes."""
    import pytesseract

    data = pytesseract.image_to_data(
        picture, lang="rus+eng", config="--oem 1 --psm 3",
        output_type=pytesseract.Output.DICT,
    )
    scale_x = float(page.rect.width) / picture.width
    scale_y = float(page.rect.height) / picture.height
    lines = []
    key = None
    words = []
    coords = []

    def flush():
        if not words:
            return
        box = [
            round(min(c[0] for c in coords) * scale_x, 2),
            round(min(c[1] for c in coords) * scale_y, 2),
            round(max(c[2] for c in coords) * scale_x, 2),
            round(max(c[3] for c in coords) * scale_y, 2),
        ]
        lines.append((" ".join(words), box, key[:2]))

    for index, raw in enumerate(data["text"]):
        word = (raw or "").strip()
        if not word:
            continue
        line_key = tuple(
            int(data[field][index])
            for field in ("block_num", "par_num", "line_num")
        )
        if key is not None and line_key != key:
            flush()
            words = []
            coords = []
        key = line_key
        words.append(word)
        left = float(data["left"][index])
        top = float(data["top"][index])
        width = float(data["width"][index])
        height = float(data["height"][index])
        coords.append((left, top, left + width, top + height))
    flush()
    return lines


def _pdf_block_type(text: str, toc_mode: bool, level: int | None) -> str:
    if _PAGE_FOOTER.match(text):
        return "page_furniture"
    if toc_mode or _CONTENTS_LINE.search(text):
        return "toc"
    if level is not None:
        return "heading"
    return "paragraph"


def _ocr_page_blocks(page, picture, page_idx, sections, toc_mode):
    lines = _ocr_lines(page, picture, page_idx)
    output = []
    pending = None

    def flush():
        nonlocal pending
        if pending is not None:
            output.append(pending)
        pending = None

    for content, box, para_key in lines:
        text = content.strip()
        if not text:
            continue

        if text.casefold() in {"содержание", "оглавление"}:
            flush()
            sections.clear()
            toc_mode = True
            output.append((text, "toc", None, box))
            continue

        level = _ocr_heading_level(text)
        if toc_mode and level == 1 and _ROOT.match(text):
            # The body starts at an uppercase numbered root, not an entry in
            # the contents. A table of contents has no true source sections.
            toc_mode = False

        if level is not None and not toc_mode:
            flush()
            sections[level] = text
            for other in list(sections):
                if other > level:
                    del sections[other]

        block_type = _pdf_block_type(text, toc_mode, level)
        section = " / ".join(sections[i] for i in sorted(sections)) or None

        # Tesseract paragraph IDs join wrapped lines. Never swallow a new
        # requirement, bullet, heading or page footer in the previous block.
        separate = (
            block_type != "paragraph"
            or _NUMBERED_LINE.match(text)
            or _BULLET_LINE.match(text)
            or pending is None
            or pending[1] != "paragraph"
            or pending[2] != section
            or pending[4] != para_key
        )
        if separate:
            flush()
            pending = (text, block_type, section, box, para_key)
        else:
            prev_text, prev_kind, prev_section, prev_box, prev_key = pending
            pending = (
                prev_text + " " + text, prev_kind, prev_section,
                [min(prev_box[0], box[0]), min(prev_box[1], box[1]),
                 max(prev_box[2], box[2]), max(prev_box[3], box[3])],
                prev_key,
            )

    flush()
    return [
        ExtractedBlock(
            page_idx, position, text, kind,
            None if kind == "toc" else section, box, "ocr",
        )
        for position, (text, kind, section, box, _) in enumerate(output)
    ], toc_mode


def _scan_dominates_page(page) -> bool:
    """Detect a scanned image with only a partial native text overlay."""
    for info in page.get_images(full=True):
        for rect in page.get_image_rects(info[0]):
            if rect.get_area() >= 0.55 * page.rect.get_area():
                return True
    return False


def extract_blocks(data: bytes, extension: str) -> list[ExtractedBlock]:
    blocks: list[ExtractedBlock] = []
    if extension == ".pdf":
        sections: dict[int, str] = {}
        toc_mode = False
        with fitz.open(stream=data, filetype="pdf") as document:
            for page_idx, page in enumerate(document, start=1):
                native = [
                    (entry[4].strip(), [round(float(coord), 2) for coord in entry[:4]])
                    for entry in page.get_text("blocks", sort=True)
                    if len(entry) > 6 and entry[6] == 0 and entry[4].strip()
                ]
                native_chars = sum(len(text) for text, _ in native)
                should_ocr = native_chars < 30
                if not should_ocr and native_chars < 180:
                    should_ocr = _scan_dominates_page(page)

                if should_ocr:
                    from PIL import Image
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    picture = Image.frombytes(
                        "RGB", (pix.width, pix.height), pix.samples,
                    )
                    extracted, toc_mode = _ocr_page_blocks(
                        page, picture, page_idx, sections, toc_mode,
                    )
                    blocks.extend(extracted)
                    continue

                for pos, (content, bbox) in enumerate(native):
                    # A digital PDF already has text boundaries provided by
                    # MuPDF; we do not modify the original source string.
                    stripped = content.strip()
                    block_type = "paragraph"
                    if _PAGE_FOOTER.fullmatch(stripped):
                        block_type = "page_furniture"
                    level = _ocr_heading_level(stripped) if "\n" not in stripped else None
                    if level:
                        block_type = "heading"
                        sections[level] = stripped
                        for other in list(sections):
                            if other > level:
                                del sections[other]
                    section = " / ".join(
                        sections[i] for i in sorted(sections)
                    ) or None
                    blocks.append(ExtractedBlock(
                        page_idx, pos, content, block_type, section, bbox, "text",
                    ))
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
