"""Only geometrically closed native PDF tables may return candidate cells."""
from io import BytesIO

import fitz
from PIL import Image

from app.documents.pdf_vector_grid import extract_closed_two_column_grid


def _native_grid(*, missing_middle=False, third_column=False,
                 merged_row=False, crossing_word=False):
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    xs = [40, 280, 560] if not third_column else [40, 200, 370, 560]
    for y in (100, 150, 200):
        page.draw_line((xs[0], y), (xs[-1], y), color=(0, 0, 0), width=.5)
    for index, x in enumerate(xs):
        if missing_middle and index == 1:
            continue
        if merged_row and index == 1:
            page.draw_line((x, 100), (x, 150), color=(0, 0, 0), width=.5)
        else:
            page.draw_line((x, 100), (x, 200), color=(0, 0, 0), width=.5)
    page.insert_text((55, 125), "9. Generator power", fontsize=12)
    page.insert_text((275 if crossing_word else 295, 125), "8.6 MW", fontsize=12)
    page.insert_text((55, 175), "10. Seismic", fontsize=12)
    page.insert_text((295, 175), "9 points", fontsize=12)
    return document


def test_two_column_grid_keeps_power_and_seismicity_in_distinct_rows():
    doc = _native_grid()
    cells = extract_closed_two_column_grid(doc[0])
    assert len(cells) == 4
    assert [(cell.row, cell.column) for cell in cells] == [
        (1, 0), (1, 1), (2, 0), (2, 1),
    ]
    assert "Generator power" in cells[0].text
    assert "8.6 MW" in cells[1].text
    assert "Seismic" in cells[2].text
    assert "9 points" in cells[3].text


def test_open_column_boundary_never_proves_cell_membership():
    doc = _native_grid(missing_middle=True)
    assert extract_closed_two_column_grid(doc[0]) == []


def test_unexpected_three_column_layout_fails_closed():
    doc = _native_grid(third_column=True)
    assert extract_closed_two_column_grid(doc[0]) == []


def test_merged_or_open_row_is_excluded_not_inferred():
    # The divider exists in row 1 only; row 2 has no verified columns.
    doc = _native_grid(merged_row=True)
    cells = extract_closed_two_column_grid(doc[0])
    assert [(cell.row, cell.column) for cell in cells] == [(1, 0), (1, 1)]


def test_word_crossing_column_divider_rejects_affected_row():
    doc = _native_grid(crossing_word=True)
    cells = extract_closed_two_column_grid(doc[0])
    assert [(cell.row, cell.column) for cell in cells] == [(2, 0), (2, 1)]


def test_raster_table_is_not_mistaken_for_vector_grid():
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    img = Image.new("RGB", (600, 800), color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    page.insert_image(page.rect, stream=buffer.getvalue())
    assert extract_closed_two_column_grid(page) == []
