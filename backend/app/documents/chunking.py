"""Section-aware deterministic chunks with exact offsets to source fragments."""
from dataclasses import dataclass

CHUNKING_VERSION = "chunks-v3"
MAX_CHARS = 1400


@dataclass(frozen=True)
class Piece:
    fragment: object
    start: int
    end: int


@dataclass(frozen=True)
class ChunkDraft:
    text: str
    section_path: str | None
    pieces: list[Piece]

    @property
    def page_start(self) -> int:
        return min(p.fragment.page for p in self.pieces)

    @property
    def page_end(self) -> int:
        return max(p.fragment.page for p in self.pieces)


def split_text(text: str, size: int = MAX_CHARS):
    """Split oversized source blocks, preferring sentences and table cells."""
    begin = 0
    while begin < len(text):
        end = min(begin + size, len(text))
        if end < len(text):
            search_from = begin + max(size // 2, 1)
            candidates = []
            for delimiter in (". ", "; ", " | ", "\n", " "):
                at = text.rfind(delimiter, search_from, end)
                if at >= search_from:
                    candidates.append((at + len(delimiter), delimiter))
            if candidates:
                end = max(candidates, key=lambda item: (
                    item[1] != " ", item[0]
                ))[0]
        if end <= begin:
            end = min(begin + size, len(text))
        yield begin, end
        begin = end


def build_chunks(fragments: list, max_chars: int = MAX_CHARS) -> list[ChunkDraft]:
    """Group related blocks, not unrelated subsections or table/prose fragments.

    A heading is attached to the following text instead of becoming an orphan
    chunk. Every character is mapped back to its original Fragment via Piece.
    Table rows are packed together, but never joined with ordinary prose.
    """
    result: list[ChunkDraft] = []
    parts: list[str] = []
    sources: list[Piece] = []
    section = None
    length = 0
    kind = None

    def flush():
        nonlocal parts, sources, length, kind
        if sources:
            result.append(ChunkDraft("\n\n".join(parts), section, sources))
        parts, sources, length, kind = [], [], 0, None

    for fragment in fragments:
        if not fragment.text or fragment.block_type in {"toc", "page_furniture"}:
            continue

        is_heading = fragment.block_type == "heading"
        is_table = fragment.block_type == "table_row"
        has_body = any(
            piece.fragment.block_type != "heading" for piece in sources
        )
        if sources and (
            (is_heading and has_body)
            or (fragment.section_path != section and has_body)
            or (kind == "table" and not is_table and not is_heading)
            or (is_table and kind == "prose")
        ):
            flush()

        # A group of consecutive headings inherits the most specific section.
        section = fragment.section_path
        for start, end in split_text(fragment.text, max_chars):
            part = fragment.text[start:end]
            if sources and length + 2 + len(part) > max_chars:
                flush()
            parts.append(part)
            sources.append(Piece(fragment, start, end))
            length += (2 if len(parts) > 1 else 0) + len(part)
            if not is_heading:
                kind = "table" if is_table else "prose"

    flush()
    return result
