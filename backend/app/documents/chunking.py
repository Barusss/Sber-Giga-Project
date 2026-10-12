"""Deterministic chunk generation with exact offsets back to OCR fragments."""
from dataclasses import dataclass


CHUNKING_VERSION = "chunks-v1"
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
    """Do not lose text even when an OCR paragraph is very long."""
    begin = 0
    while begin < len(text):
        end = min(begin + size, len(text))
        if end < len(text):
            boundary = max(text.rfind("\n", begin + size // 2, end), text.rfind(" ", begin + size // 2, end))
            if boundary > begin:
                end = boundary + 1
        yield begin, end
        begin = end


def build_chunks(fragments: list, max_chars: int = MAX_CHARS) -> list[ChunkDraft]:
    """Combine neighboring blocks; split oversized ones, preserving source spans."""
    result: list[ChunkDraft] = []
    parts: list[str] = []
    sources: list[Piece] = []
    section = None
    length = 0

    def flush():
        nonlocal parts, sources, length
        if sources:
            result.append(ChunkDraft("\n\n".join(parts), section, sources))
        parts, sources, length = [], [], 0

    for fragment in fragments:
        if not fragment.text:
            continue
        if sources and (fragment.block_type == "heading" or fragment.section_path != section):
            flush()
        section = fragment.section_path
        for start, end in split_text(fragment.text, max_chars):
            part = fragment.text[start:end]
            separator = 2 if parts else 0
            if sources and length + separator + len(part) > max_chars:
                flush()
            parts.append(part)
            sources.append(Piece(fragment, start, end))
            length += (2 if len(parts) > 1 else 0) + len(part)
    flush()
    return result
