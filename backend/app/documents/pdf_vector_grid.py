"""Strict proof-of-geometry for native two-column PDF grids.

Diagnostics only: returns no cells for OCR/rasterized, open, merged, unusual,
or ambiguous table borders. Geometry is not evidence of engineering coverage:
no cell may become a verified requirement without a separate source binding.
"""
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class VectorCell:
    row: int
    column: int
    bbox: tuple[float, float, float, float]
    words: tuple[tuple[float, float, float, float, str], ...]

    @property
    def text(self) -> str:
        return " ".join(
            item[4] for item in sorted(
                self.words, key=lambda word: (round(word[1] / 3), word[0]),
            )
        )


def _distinct(values: Sequence[float], tolerance: float = 1.1) -> list[float]:
    unique: list[float] = []
    for value in sorted(values):
        if not unique or abs(unique[-1] - value) > tolerance:
            unique.append(value)
    return unique


def _border_segments(page):
    """A native PDF may encode rules as thin filled rects or stroked lines."""
    horizontals, verticals = [], []
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "re":
                rect = item[1]
                if rect.height <= 1.8 and rect.width >= 30:
                    horizontals.append(((rect.y0 + rect.y1) / 2, rect.x0, rect.x1))
                if rect.width <= 1.8 and rect.height >= 10:
                    verticals.append(((rect.x0 + rect.x1) / 2, rect.y0, rect.y1))
            elif item[0] == "l":
                start, end = item[1:3]
                if abs(start.y - end.y) < 0.6 and abs(start.x - end.x) >= 30:
                    horizontals.append((
                        (start.y + end.y) / 2,
                        min(start.x, end.x), max(start.x, end.x),
                    ))
                if abs(start.x - end.x) < 0.6 and abs(start.y - end.y) >= 10:
                    verticals.append((
                        (start.x + end.x) / 2,
                        min(start.y, end.y), max(start.y, end.y),
                    ))
    return horizontals, verticals


def _covered(
    spans: Sequence[tuple[float, float]], start: float, end: float,
    tolerance: float = 1.6,
) -> bool:
    reached = start
    for low, high in sorted(spans):
        if high <= reached:
            continue
        if low > reached + tolerance:
            return False
        reached = max(reached, high)
        if reached >= end - tolerance:
            return True
    return False


def extract_closed_two_column_grid(page) -> list[VectorCell]:
    """Return candidates only when two cells of each row have four real borders.

    This intentionally handles just one strict three-vertical-line grid per
    page, not inferred columns or a multipage table. Every source word must
    fit entirely in a single cell. No PDF fragment IDs are assigned here.
    Returns [] (unknown) rather than guessing if evidence is insufficient.
    """
    horizontal, vertical = _border_segments(page)
    xs = _distinct([x for x, _, _ in vertical])
    ys = _distinct([y for y, _, _ in horizontal])
    if len(xs) != 3 or len(ys) < 3:
        return []

    left, middle, right = xs
    if not left + 25 < middle < right - 25:
        return []

    words = page.get_text("words")
    output: list[VectorCell] = []
    for row_index, (top, bottom) in enumerate(zip(ys, ys[1:]), start=1):
        if bottom - top < 12:
            continue
        if not all(
            _covered(
                [(lo, hi) for x, lo, hi in vertical if abs(x - edge) < 1.1],
                top, bottom,
            ) for edge in xs
        ):
            continue
        if not all(
            _covered(
                [(lo, hi) for y, lo, hi in horizontal if abs(y - edge) < 1.1],
                left, right,
            ) for edge in (top, bottom)
        ):
            continue

        cell_words: list[list[tuple[float, float, float, float, str]]] = [[], []]
        ambiguous = False
        for word in words:
            x0, y0, x1, y1 = word[:4]
            if not (top < (y0 + y1) / 2 < bottom
                    and left < (x0 + x1) / 2 < right):
                continue
            if y0 < top - 1 or y1 > bottom + 1:
                ambiguous = True
                break
            if x0 >= left - 1 and x1 <= middle + 1:
                cell_words[0].append((x0, y0, x1, y1, word[4]))
            elif x0 >= middle - 1 and x1 <= right + 1:
                cell_words[1].append((x0, y0, x1, y1, word[4]))
            else:
                ambiguous = True
                break
        if ambiguous or not cell_words[0] or not cell_words[1]:
            continue

        for column in range(2):
            output.append(VectorCell(
                row_index, column,
                (xs[column], top, xs[column + 1], bottom),
                tuple(cell_words[column]),
            ))
    return output
