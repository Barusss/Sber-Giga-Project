"""Deterministic extraction of atomic clauses from document fragments.

Fragments are the smallest traceable unit produced by the document pipeline.
This module groups them into numbered clauses so that every requirement and
every ZnP match can point to a real page and fragment instead of a chunk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Sequence

from app.core.models import Fragment

MAX_CLAUSE_CHARS = 4000
MIN_PREAMBLE_CHARS = 40
MAX_CLAUSE_NUMBER_CHARS = 32

# Fragments that only repeat page furniture and must never become clauses.
NOISE_PATTERNS = (
    r"^\s*(?:стр(?:аница)?\.?\s*)?\d{1,4}(?:\s*(?:из|/)\s*\d{1,4})?\s*$",
    r"^[\s\-_=—–.·•*]+$",
    r"^\s*(?:подпись|подписи|м\.?\s*п\.?|место\s+печати|"
    r"место\s+постановки\s+печати)\b.*$",
)
_NOISE_RE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in NOISE_PATTERNS)

# Formal TU sections that describe the document itself rather than a requirement.
SERVICE_PATTERNS = (
    r"^\s*основани[ея]\b",
    r"^\s*срок\s+(?:действия|оказания)\b",
    r"^\s*приложени[ея]\b",
    r"^\s*примечани[ея]\b",
    r"^\s*(?:утверждаю|согласовано)\b",
    r"^\s*задание\s+на\s+проектирование\b",
    r"^\s*технические\s+условия\b",
    r"^\s*(?:содержание|оглавление)\s*$",
    r"^\s*(?:перечень|список)\s+(?:сокращени|обозначени)\b",
    r"^\s*перечень\s+нормативн\w*\s+документ\w*",
    r"^\s*термины\s+и\s+определения\b",
    r"^\s*условные\s+обозначения\b",
)
_SERVICE_RE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in SERVICE_PATTERNS)

# Table-of-contents rows and headings that never contain engineering requirements.
_TOC_LINE_RE = re.compile(
    r"(?:\.{3,}|…{2,}|·{3,}|_{3,}|-{3,})\s*\d{1,4}\s*$"
)
_TOC_TITLE_RE = re.compile(r"^\s*(?:содержание|оглавление)\s*[.:]?\s*$", re.IGNORECASE)
_IGNORED_HEADING_PATTERNS = (
    r"^\s*перечень\s+(?:сокращени|обозначени|нормативн|условных)",
    r"^\s*(?:список|перечень)\s+(?:сокращени|обозначени)",
    r"^\s*термины\s+и\s+определения\b",
    r"^\s*условные\s+обозначения\b",
)
_IGNORED_HEADING_RE = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in _IGNORED_HEADING_PATTERNS
)
_PREAMBLE_RE = re.compile(
    r"\b(?:настоящ\w*|требовани\w*|распространя\w*|определя\w*|"
    r"устанавлива\w*|предъявля\w*|должн\w*|необходим\w*)\b",
    re.IGNORECASE,
)
_TITLE_PREAMBLE_RE = re.compile(
    r"(?im)^\s*(?:утверждаю|согласовано|задание\s+на\s+проектирование|"
    r"технические\s+условия|номер\s+ту|дата\s+выдачи)\b"
)

_NUMBER_START_RE = re.compile(
    r"^\s*(?:п\.?\s*|пункт\s*)?(\d{1,2}(?:\.\d{1,2}){0,5})[.)]?\s+",
    re.IGNORECASE,
)
_NUMBER_PREFIX_RE = re.compile(
    r"^\s*(?:п\.?\s*|пункт\s*)?\d{1,2}(?:\.\d{1,2}){0,5}[.)]?\s*",
    re.IGNORECASE,
)
_SECTION_HEADING_RE = re.compile(
    r"^\s*((?:раздел|section)\s+(?:\d{1,3}|[ivxlcdm]+))(?=\s|$|[.:])",
    re.IGNORECASE,
)
_HEADING_NUMBER_RE = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){0,5})[.)]?\s*(.*)$")
_HEADING_LABEL_PREFIX_RE = re.compile(
    r"^\s*\d{1,3}(?:\.\d{1,3}){0,5}[.)]?\s+"
)


@dataclass(frozen=True)
class Clause:
    """A numbered atomic clause assembled from one or more fragments."""

    clause_number: str
    text: str
    page: int
    fragment_id: str | None
    fragment_ids: list[str]
    section_path: str | None
    fragment_text: str


@dataclass
class _Draft:
    clause_number: str
    sections: list[tuple[Fragment, str]]
    section_path: str | None

    @property
    def text(self) -> str:
        return "\n".join(text for _, text in self.sections)


def is_noise(text: str) -> bool:
    """Return True for page numbers, separators and signature placeholders."""
    stripped = text.strip()
    if len(stripped) < 3:
        return True
    return any(pattern.match(stripped) for pattern in _NOISE_RE)


def is_service_clause(text: str) -> bool:
    """Recognise formal TU sections that are not engineering requirements."""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return any(pattern.match(first_line) for pattern in _SERVICE_RE)


def is_toc_line(text: str) -> bool:
    """Return True for a table-of-contents row with dot leaders and a page number."""
    return bool(_TOC_LINE_RE.search(text.strip()))


def normalize_clause_text(text: str) -> str:
    """Build a deduplication key independent of numbering and whitespace."""
    normalized = text.replace("ё", "е").replace("Ё", "Е").casefold()
    normalized = _NUMBER_PREFIX_RE.sub("", normalized, count=1)
    return re.sub(r"\s+", " ", normalized).strip()


def extract_clauses(fragments: Sequence[Fragment]) -> list[Clause]:
    """Group ordered fragments into atomic, deduplicated clauses.

    Numbered lines start clauses. Headings never become clauses by themselves:
    a heading supplies its number and section path to the first unnumbered body
    below it. Headings also close the previous clause, so a heading between two
    points cannot glue the next text to the previous one. Table-of-contents and
    service headings are skipped together with their unnumbered content.
    """
    ordered = sorted(
        (fragment for fragment in fragments if (fragment.text or "").strip()),
        key=lambda fragment: (fragment.page, fragment.position),
    )

    drafts: list[_Draft] = []
    preamble: _Draft | None = None
    current: _Draft | None = None
    pending_heading: Fragment | None = None
    toc_mode = False
    skip_section = False

    for index, fragment in enumerate(ordered):
        text = fragment.text.strip()
        if is_noise(text):
            continue
        block_type = fragment.block_type or "paragraph"

        if _TOC_TITLE_RE.match(text):
            current = None
            pending_heading = None
            toc_mode = True
            skip_section = False
            continue

        if is_toc_line(text):
            current = None
            pending_heading = None
            continue

        if toc_mode:
            if (
                _looks_like_toc_entry(text, block_type)
                and not _next_is_body(ordered, index)
            ):
                current = None
                if _is_ignored_heading(text):
                    pending_heading = None
                    skip_section = True
                elif block_type == "heading" or _start_clause_number(text):
                    pending_heading = fragment
                continue
            toc_mode = False
            if _is_unlabelled_body_heading(fragment, ordered, index):
                current = None
                pending_heading = fragment
                continue

        if block_type == "heading":
            current = None
            if _is_ignored_heading(text):
                pending_heading = None
                skip_section = True
            else:
                pending_heading = fragment
                skip_section = False
            continue

        if _is_ignored_heading(text) and _looks_like_heading(text):
            current = None
            pending_heading = None
            skip_section = True
            continue

        clause_number = _start_clause_number(text)
        if skip_section:
            # A real subsection ends a service/TOC block; table rows and
            # single-level appendix entries stay excluded.
            if clause_number and "." in clause_number and block_type != "table_row":
                skip_section = False
            else:
                current = None
                pending_heading = None
                continue

        if clause_number is not None:
            current = _Draft(
                clause_number=clause_number,
                sections=[(fragment, text)],
                section_path=fragment.section_path,
            )
            drafts.append(current)
            pending_heading = None
        elif pending_heading is not None:
            current = _Draft(
                clause_number=_heading_clause_number(pending_heading.text),
                sections=[(fragment, text)],
                section_path=(
                    pending_heading.section_path
                    or " ".join(pending_heading.text.split())
                ),
            )
            drafts.append(current)
            pending_heading = None
        elif current is not None:
            current.sections.append((fragment, text))
        elif not drafts:
            if preamble is None:
                preamble = _Draft(
                    clause_number="Преамбула",
                    sections=[(fragment, text)],
                    section_path=fragment.section_path,
                )
            else:
                preamble.sections.append((fragment, text))

    if preamble is not None and _keep_preamble(preamble):
        drafts.insert(0, preamble)

    clauses: list[Clause] = []
    for draft in drafts:
        clauses.extend(_draft_to_clauses(draft))

    deduplicated: list[Clause] = []
    seen: set[str] = set()
    for clause in clauses:
        key = normalize_clause_text(clause.text)
        if not key or key in seen:
            continue
        seen.add(key)
        deduplicated.append(clause)

    numbered: list[Clause] = []
    number_counts: dict[str, int] = {}
    for clause in deduplicated:
        count = number_counts.get(clause.clause_number, 0) + 1
        number_counts[clause.clause_number] = count
        if count > 1:
            clause = replace(
                clause,
                clause_number=_with_suffix(clause.clause_number, f"#{count}"),
            )
        numbered.append(clause)
    return numbered


def _start_clause_number(text: str) -> str | None:
    section = _SECTION_HEADING_RE.match(text)
    if section:
        return section.group(1).strip()[:MAX_CLAUSE_NUMBER_CHARS]

    number = _NUMBER_START_RE.match(text)
    if number:
        return number.group(1)[:MAX_CLAUSE_NUMBER_CHARS]
    return None


def _heading_clause_number(text: str) -> str:
    """Use the heading's own number, or its label when it has no number."""
    cleaned = " ".join(text.split())
    section = _SECTION_HEADING_RE.match(cleaned)
    if section:
        return section.group(1).strip()[:MAX_CLAUSE_NUMBER_CHARS]
    number = _HEADING_NUMBER_RE.match(cleaned)
    if number:
        return number.group(1)[:MAX_CLAUSE_NUMBER_CHARS]
    return (cleaned or "Заголовок")[:MAX_CLAUSE_NUMBER_CHARS]


def _heading_label(text: str) -> str:
    """Strip a leading section number so service headings are recognised."""
    cleaned = " ".join(text.split())
    section = _SECTION_HEADING_RE.match(cleaned)
    if section:
        remainder = cleaned[section.end():].lstrip(" .:—-")
        return remainder or cleaned
    number = _HEADING_LABEL_PREFIX_RE.match(cleaned)
    if number:
        return cleaned[number.end():] or cleaned
    return cleaned


def _is_ignored_heading(text: str) -> bool:
    label = _heading_label(text)
    return any(pattern.match(label) for pattern in _IGNORED_HEADING_RE) or (
        is_service_clause(label)
    )


def _looks_like_heading(text: str) -> bool:
    stripped = " ".join(text.split())
    return len(stripped) <= 160 and not stripped.endswith((".", "!", "?", ";"))


def _looks_like_toc_entry(text: str, block_type: str) -> bool:
    if block_type == "heading":
        return True
    stripped = text.strip()
    if is_toc_line(stripped):
        return True
    if stripped.endswith((".", "!", "?", ";")):
        return False
    if _start_clause_number(stripped) is not None:
        return True
    return bool(re.match(r"^\s*(?:приложение|перечень|термины|условные)\b",
                         stripped, re.IGNORECASE))


def _next_is_body(fragments: Sequence[Fragment], index: int) -> bool:
    """Look one step ahead: TOC entries are followed by entries, not by prose."""
    for fragment in fragments[index + 1:]:
        text = (fragment.text or "").strip()
        if not text or is_noise(text) or is_toc_line(text) or _TOC_TITLE_RE.match(text):
            continue
        if _looks_like_toc_entry(text, fragment.block_type or "paragraph"):
            return False
        return len(text) >= 100 or text.endswith((".", "!", "?", ";"))
    return False


def _is_unlabelled_body_heading(
    fragment: Fragment, fragments: Sequence[Fragment], index: int,
) -> bool:
    """A numbered TOC-like line followed by prose is a real subsection heading."""
    if fragment.block_type == "heading":
        return False
    text = (fragment.text or "").strip()
    return (
        not text.endswith((".", "!", "?", ";"))
        and _start_clause_number(text) is not None
        and _next_is_body(fragments, index)
    )


def _keep_preamble(draft: _Draft) -> bool:
    """Keep only a substantive preamble, not a title page or document metadata."""
    text = draft.text
    if len(text) < MIN_PREAMBLE_CHARS:
        return False
    if _TITLE_PREAMBLE_RE.search(text):
        return False
    return bool(_PREAMBLE_RE.search(text)) or is_service_clause(text)


def _draft_to_clauses(draft: _Draft) -> list[Clause]:
    text = draft.text
    if len(text) <= MAX_CLAUSE_CHARS:
        first_fragment, first_text = draft.sections[0]
        return [
            Clause(
                clause_number=draft.clause_number,
                text=text,
                page=first_fragment.page,
                fragment_id=first_fragment.id,
                fragment_ids=_fragment_ids(draft.sections),
                section_path=draft.section_path,
                fragment_text=first_text,
            )
        ]

    parts = _split_long_text(text)
    spans: list[tuple[int, int, Fragment]] = []
    offset = 0
    for fragment, fragment_text in draft.sections:
        spans.append((offset, offset + len(fragment_text), fragment))
        offset += len(fragment_text) + 1  # joined with "\n"

    clauses: list[Clause] = []
    search_from = 0
    for index, part in enumerate(parts, start=1):
        part_start = text.find(part, search_from)
        if part_start < 0:  # pragma: no cover - defensive fallback
            part_start = search_from
        part_end = part_start + len(part)
        search_from = part_end

        overlapping = [
            fragment
            for start, end, fragment in spans
            if start < part_end and end > part_start
        ]
        first_fragment = overlapping[0] if overlapping else draft.sections[0][0]
        first_text = overlapping[0].text.strip() if overlapping else draft.sections[0][1]
        suffix = f" (ч. {index}/{len(parts)})"
        clauses.append(
            Clause(
                clause_number=_with_suffix(draft.clause_number, suffix),
                text=part,
                page=first_fragment.page,
                fragment_id=first_fragment.id,
                fragment_ids=_unique_ids(overlapping) or _fragment_ids(draft.sections),
                section_path=draft.section_path,
                fragment_text=first_text,
            )
        )
    return clauses


def _split_long_text(text: str) -> list[str]:
    """Split by paragraph boundaries first, then by sentence/word if needed.

    Every returned part is an exact substring of ``text`` so that the caller
    can map it back to the originating fragments.
    """
    parts: list[str] = []
    current = ""

    for paragraph in text.split("\n"):
        if not paragraph.strip():
            continue
        if len(paragraph) > MAX_CLAUSE_CHARS:
            if current:
                parts.append(current)
                current = ""
            parts.extend(_hard_split(paragraph))
            continue

        candidate = f"{current}\n{paragraph}" if current else paragraph
        if len(candidate) <= MAX_CLAUSE_CHARS:
            current = candidate
        else:
            if current:
                parts.append(current)
            current = paragraph

    if current:
        parts.append(current)
    return parts or [text[:MAX_CLAUSE_CHARS]]


def _hard_split(paragraph: str) -> list[str]:
    parts: list[str] = []
    remaining = paragraph
    while len(remaining) > MAX_CLAUSE_CHARS:
        cut = remaining.rfind(". ", 0, MAX_CLAUSE_CHARS)
        if cut < MAX_CLAUSE_CHARS // 2:
            cut = remaining.rfind(" ", 0, MAX_CLAUSE_CHARS)
        if cut < MAX_CLAUSE_CHARS // 2:
            cut = MAX_CLAUSE_CHARS
        piece = remaining[:cut]
        if piece.strip():
            parts.append(piece)
        remaining = remaining[cut:].lstrip()
    if remaining.strip():
        parts.append(remaining)
    return parts


def _fragment_ids(sections: list[tuple[Fragment, str]]) -> list[str]:
    return _unique_ids([fragment for fragment, _ in sections])


def _unique_ids(fragments: Sequence[Fragment]) -> list[str]:
    result: list[str] = []
    for fragment in fragments:
        if fragment.id is not None and fragment.id not in result:
            result.append(fragment.id)
    return result


def _with_suffix(clause_number: str, suffix: str) -> str:
    limit = max(1, MAX_CLAUSE_NUMBER_CHARS - len(suffix))
    return f"{clause_number[:limit].rstrip()}{suffix}"
