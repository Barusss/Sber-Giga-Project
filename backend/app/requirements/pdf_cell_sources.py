"""Map native PDF words to exact offsets in existing source fragments."""
import re

import fitz

from app.documents.pdf_vector_grid import extract_closed_two_column_grid
from app.requirements.atomization import AtomSource


def bind_pdf_cells(data, fragments):
    """Return verified cell spans and failures, preserving original fragment IDs."""
    by_page = {}
    by_id = {}
    for fragment in fragments:
        by_page.setdefault(fragment.page, []).append(fragment)
        by_id[fragment.id] = fragment

    results = []
    try:
        document = fitz.open(stream=data, filetype="pdf")
    except (fitz.FileDataError, fitz.EmptyFileError):
        return results
    with document:
        for page_index, page in enumerate(document, 1):
            cells = extract_closed_two_column_grid(page)
            if not cells:
                continue
            blocks = page.get_text("blocks", sort=True)
            words = page.get_text("words")
            mapped_words = {}
            for block_index, block in enumerate(blocks):
                if len(block) < 7 or block[6] != 0:
                    continue
                text = block[4].strip()
                candidates = [
                    f for f in by_page.get(page_index, [])
                    if f.text == text and f.bbox and all(
                        abs(float(a) - float(b)) < 0.05
                        for a, b in zip(f.bbox, block[:4])
                    )
                ]
                if len(candidates) != 1:
                    continue
                fragment = candidates[0]
                block_words = [w for w in words if w[5] == block[5]]
                tokens = list(re.finditer(r"\S+", fragment.text))
                if [m.group() for m in tokens] != [w[4] for w in block_words]:
                    continue
                for word, token in zip(block_words, tokens):
                    key = (tuple(round(float(v), 3) for v in word[:4]), word[4])
                    mapped_words.setdefault(key, []).append(
                        AtomSource(fragment.id, fragment.page, token.start(), token.end(), token.group())
                    )

            for cell in cells:
                spans = []
                reason = None
                for word in cell.words:
                    key = (tuple(round(float(v), 3) for v in word[:4]), word[4])
                    candidates = mapped_words.get(key, [])
                    if len(candidates) != 1:
                        reason = "word_mapping_ambiguous_or_missing"
                        break
                    source = candidates[0]
                    if spans and spans[-1].fragment_id == source.fragment_id:
                        previous = spans[-1]
                        raw = by_id[source.fragment_id].text
                        between = raw[previous.end_offset:source.start_offset]
                        if source.start_offset >= previous.end_offset and (not between or between.isspace()):
                            spans[-1] = AtomSource(
                                source.fragment_id, source.page, previous.start_offset,
                                source.end_offset, raw[previous.start_offset:source.end_offset],
                            )
                            continue
                    spans.append(source)
                results.append((page_index, cell, tuple(spans) if reason is None else (), reason))
    return results
