"""Инструменты ИИ-агента для сверки требований ТУ и пунктов ЗнП."""
import math
import re
from typing import Any

from app.requirements.schemas import RequirementCategory, ZNPClause

_MAX_ATOMIC_QUOTE_CHARS = 600
_TYPOGRAPHY = str.maketrans({
    "«": '"', "»": '"', "„": '"', "“": '"', "”": '"', "‟": '"',
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "–": "-", "—": "-", "−": "-", "‒": "-", "―": "-", "‑": "-",
    "…": "...",
    "\u00a0": " ", "\u2009": " ", "\u202f": " ", "\u200b": "",
})
_STOPWORDS = frozenset({
    "для", "при", "или", "как", "что", "это", "этот", "эта", "эти",
    "того", "том", "также", "более", "менее", "через", "между", "них",
    "него", "нее", "его", "ее", "их", "же", "ли", "бы", "то", "все",
    "всех", "который", "которая", "которые", "которых", "быть", "есть",
})
_TOKEN_RE = re.compile(r"[а-яa-z0-9]{2,}", re.IGNORECASE)
_CLAUSE_NUMBER_RE = re.compile(
    r"^\s*(?:п\.?\s*|пункт\s*)?\d{1,2}(?:\.\d{1,2}){1,5}[.)]?\s+",
    re.IGNORECASE,
)
_IDENTIFIER_PATTERNS = (
    re.compile(
        r"\b(?:гост(?:\s+р)?|сп|снип|санпин|фз|пуэ|птээп|птэ|пб|рд|всн|"
        r"гк|грк|зк|нк|коап)\s*[-№]?\s*\d[\w/-]*(?:\.[\w/-]+)*",
        re.IGNORECASE,
    ),
    re.compile(r"№\s*\d[\w/-]*(?:\.[\w/-]+)*", re.IGNORECASE),
    re.compile(r"\b[а-яё]{2,6}[-–]?\d{1,4}(?:[.,/]\d+)?\b", re.IGNORECASE),
    re.compile(
        r"\b\d+(?:[.,]\d+)?\s*(?:квт|мвт|ква|мва|кв|в|а|мм2|мм²|м|шт|%|"
        r"гц|ом|мвар|квар)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b\d+(?:[.,]\d+)?[sS]\b"),
    re.compile(r"\b(?:I{1,3}|1|2|3)\s*-?\s*категори\w*", re.IGNORECASE),
)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_LIST_MARKER_RE = re.compile(
    r"(?:^|[\n;:])\s*(?:"
    r"[-–—•▪◦●]"
    r"|\d{1,3}(?:\.\d{1,3}){0,5}[.)]"
    r"|[а-яa-z][.)]"
    r"|[ivxlcdm]{1,4}[.)]"
    r")\s+",
    re.IGNORECASE | re.MULTILINE,
)
_ATOMIC_MARKER_RE = re.compile(
    r"(?:^|[\n;])\s*(?:"
    r"[-–—•▪◦●]"
    r"|\d{1,3}(?:\.\d{1,3}){0,5}[.)]"
    r"|[а-яa-z][.)]"
    r"|[ivxlcdm]{1,4}[.)]"
    r")\s+",
    re.IGNORECASE | re.MULTILINE,
)
_EVIDENCE_BOUNDARY_RE = re.compile(
    r"\n+"
    r"|(?<=[.;!?])\s+(?=(?:\d{1,3}(?:\.\d{1,3}){0,5}[.)]\s)|[А-ЯЁA-Z«\"(])"
    r"|(?<=\s)(?=\d{1,3}(?:\.\d{1,3}){0,5}[.)]\s)",
)


def _normalize_typography(text: str) -> str:
    return text.translate(_TYPOGRAPHY).replace("Ё", "Е").replace("ё", "е")


def _normalize_space(text: str) -> str:
    text = _normalize_typography(text)
    text = re.sub(r"[\u2000-\u200b\u202f\u205f\u3000]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_quote_text(text: str) -> str:
    return _normalize_space(text).casefold()


def _canonical_text(text: str) -> str:
    """Canonical form for identifier/quote containment checks."""
    text = _normalize_space(text).casefold()
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    text = re.sub(r"(?<=\d)\.0+(?=\D|$)", "", text)
    text = re.sub(r"№\s*", "№", text)
    text = re.sub(r"\s*([-/])\s*", r"\1", text)
    text = re.sub(
        r"(\d)\s+(?=(?:квт|мвт|ква|мва|кв|в|а|мм2|мм²|м|шт|%|гц|ом|"
        r"мвар|квар)\b)",
        r"\1",
        text,
    )
    return text


def _extract_identifiers(text: str) -> set[str]:
    found: set[str] = set()
    clean = _normalize_space(text)
    for pattern in _IDENTIFIER_PATTERNS:
        for match in pattern.finditer(clean):
            value = _canonical_text(match.group(0))
            if value:
                found.add(value)
    return found


def _identifier_present(identifier: str, canonical_text: str) -> bool:
    pattern = (
        r"(?<![а-яa-z0-9])"
        + re.escape(identifier)
        + r"(?![а-яa-z0-9])"
    )
    return re.search(pattern, canonical_text) is not None


def _content_tokens(text: str) -> set[str]:
    normalized = _normalize_space(text).casefold()
    return {
        match.group(0)[:6]
        for match in _TOKEN_RE.finditer(normalized)
        if len(match.group(0)) >= 3 and match.group(0) not in _STOPWORDS
    }


def _shorten(text: str, limit: int = 160) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _is_atomic_quote(quote: str | None, max_chars: int = _MAX_ATOMIC_QUOTE_CHARS) -> bool:
    """A quote must be one short fragment, not a whole glued list."""
    text = (quote or "").strip()
    if not text or len(text) > max_chars:
        return False
    if text.count("\n") > 1:
        return False
    return len(_ATOMIC_MARKER_RE.findall(text)) < 2


def _split_explicit_list_items(text: str) -> list[str]:
    matches = list(_LIST_MARKER_RE.finditer(text))
    if len(matches) < 2:
        return []

    items: list[str] = []
    first_separator = text[matches[0].start()] if matches[0].start() < len(text) else ""
    prefix = text[: matches[0].start()].strip().rstrip(";").strip()
    if (
        prefix
        and first_separator == ";"
        and not prefix.endswith(":")
        and len(_content_tokens(prefix)) >= 2
    ):
        items.append(prefix)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        item = text[match.end():end].strip().rstrip(";").strip()
        if len(item) >= 3:
            items.append(item)
    return items


def _list_items(tu_text: str) -> list[str]:
    """Extract list items from a TU requirement or return an empty list."""
    clean = _CLAUSE_NUMBER_RE.sub("", tu_text.strip(), count=1)
    # "1) foo 2) bar" is a list even when DOCX/extraction lost line breaks.
    inline_clean = re.sub(
        r"(?<=[^\s;:\n])[ \t]+"
        r"(?=(?:\d{1,2}|[а-яa-z]|[ivxlcdm]{1,4})[)]\s+)",
        "; ",
        clean,
        flags=re.IGNORECASE,
    )

    explicit = _split_explicit_list_items(inline_clean)
    if len(explicit) >= 2:
        return explicit

    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    if len(lines) >= 3 and lines[0].rstrip().endswith(":"):
        items = [
            line.lstrip("-–—•▪◦●").strip()
            for line in lines[1:]
        ]
        items = [item for item in items if len(item) >= 3]
        if len(items) >= 2:
            return items

    # Inline list without bullets: require at least three segments and at least
    # two identifiers, otherwise an ordinary sentence with a semicolon becomes a list.
    segments = [segment.strip().rstrip(";").strip() for segment in clean.split(";")]
    segments = [segment for segment in segments if len(segment) >= 3]
    if len(segments) >= 3:
        identified = sum(1 for segment in segments if _extract_identifiers(segment))
        if identified >= 2:
            return segments
    return []


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = math.fsum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(math.fsum(a * a for a in left))
    right_norm = math.sqrt(math.fsum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


# ---------------------------------------------------------------------------
# ИНСТРУМЕНТ 1: Поиск подходящих пунктов ЗнП (лексика + опциональная семантика)
# ---------------------------------------------------------------------------
_LEADING_CLAUSE_NUMBER_RE = re.compile(
    r"^\s*(?:п\.?\s*|пункт\s*)?\d{1,2}(?:\.\d{1,2}){0,5}[.)]\s*",
    re.IGNORECASE,
)


def _strip_clause_number(text: str) -> str:
    """Убирает номер пункта, чтобы «4.2.» не давал ложное совпадение с другим 4.2."""
    return _LEADING_CLAUSE_NUMBER_RE.sub("", text, count=1)


def tool_search_znp_clauses(
    query: str,
    znp_clauses: list[ZNPClause],
    top_k: int = 3,
    query_embedding: list[float] | None = None,
    clause_embeddings: list[list[float]] | None = None,
    semantic_weight: float = 0.6,
) -> list[dict[str, Any]]:
    """Ищет релевантные пункты ЗнП; без embeddings работает как раньше."""
    norm_query = _strip_clause_number(query).lower().replace("ё", "е")
    query_tokens = set(re.findall(r"[а-яa-z0-9]{2,}", norm_query))
    semantic_available = bool(
        query_embedding
        and clause_embeddings is not None
        and len(clause_embeddings) == len(znp_clauses)
    )
    if not query_tokens and not semantic_available:
        return []

    numbers_in_query = {m.replace(",", ".") for m in re.findall(r"\d+(?:[.,]\d+)?", norm_query)}

    scored: list[dict[str, Any]] = []
    for idx, clause in enumerate(znp_clauses):
        norm_clause = _strip_clause_number(clause.text).lower().replace("ё", "е")
        clause_tokens = set(re.findall(r"[а-яa-z0-9]{2,}", norm_clause))
        overlap = query_tokens & clause_tokens

        numbers_in_clause = {m.replace(",", ".") for m in re.findall(r"\d+(?:[.,]\d+)?", norm_clause)}
        num_bonus = 0.25 if (numbers_in_query and (numbers_in_query & numbers_in_clause)) else 0.0

        lexical_score = min((len(overlap) / max(len(query_tokens), 1)) + num_bonus, 1.0)
        semantic_score = None
        if semantic_available:
            semantic_score = max(0.0, min(1.0, _cosine(query_embedding, clause_embeddings[idx])))
            score = (
                semantic_weight * semantic_score
                + (1.0 - semantic_weight) * lexical_score
            )
        else:
            score = lexical_score

        scored.append({
            "clause_id": clause.id,
            "clause_number": clause.clause_number,
            "text": clause.text,
            "position_index": idx,
            "similarity_score": round(min(score, 1.0), 3),
            "lexical_score": round(lexical_score, 3),
            "semantic_score": (
                round(semantic_score, 3) if semantic_score is not None else None
            ),
            "channels": ["lexical", "semantic"] if semantic_available else ["lexical"],
        })

    scored.sort(key=lambda item: item["similarity_score"], reverse=True)
    return scored[:top_k]


# ---------------------------------------------------------------------------
# ИНСТРУМЕНТ 2: Расширение контекста (чтение соседних пунктов ЗнП)
# ---------------------------------------------------------------------------
def tool_get_neighbor_clauses(
    target_clause_id: str,
    znp_clauses: list[ZNPClause],
    window: int = 1,
) -> list[dict[str, Any]]:
    """Возвращает соседние пункты ЗнП (до и после целевого) для понимания полного контекста раздела."""
    target_idx = next((i for i, c in enumerate(znp_clauses) if c.id == target_clause_id), None)
    if target_idx is None:
        return []

    start = max(0, target_idx - window)
    end = min(len(znp_clauses), target_idx + window + 1)
    return [
        {
            "clause_id": c.id,
            "clause_number": c.clause_number,
            "text": c.text,
            "is_target": c.id == target_clause_id,
        }
        for c in znp_clauses[start:end]
    ]


# ---------------------------------------------------------------------------
# ИНСТРУМЕНТ 3: Детерминированный валидатор инженерных параметров
# ---------------------------------------------------------------------------
def tool_verify_engineering_params(tu_text: str, znp_text: str) -> dict[str, Any]:
    """Сверяет числовые и технические характеристики между ТУ и ЗнП."""
    mismatches: list[str] = []

    def norm_num(val: str) -> str:
        cleaned = val.replace(",", ".").strip()
        try:
            num = float(cleaned)
            return str(int(num)) if num.is_integer() else str(num)
        except ValueError:
            return cleaned

    # 1. Мощность (кВт, МВт, кВА, МВА)
    power_re = r"(\d+(?:[.,]\d+)?)\s*(кВт|МВт|кВА|МВА)\b"
    tu_power = {(norm_num(m[0]), m[1].lower()) for m in re.findall(power_re, tu_text, re.IGNORECASE)}
    znp_power = {(norm_num(m[0]), m[1].lower()) for m in re.findall(power_re, znp_text, re.IGNORECASE)}
    if tu_power and znp_power and tu_power != znp_power:
        mismatches.append(f"Несовпадение мощности: ТУ={tu_power}, ЗнП={znp_power}")
    elif tu_power and not znp_power:
        mismatches.append(f"В ЗнП не указано значение мощности из ТУ ({tu_power})")

    # 2. Класс напряжения: кВ регистронезависимо, а одиночная 'В' — только заглавная (не предлог 'в')
    volt_re = r"(\d+(?:[.,]\d+)?)\s*(?:([кК][вВ])|(В))\b"
    tu_volt = {
        (norm_num(m[0]), "кв" if m[1] else "в")
        for m in re.findall(volt_re, tu_text)
    }
    znp_volt = {
        (norm_num(m[0]), "кв" if m[1] else "в")
        for m in re.findall(volt_re, znp_text)
    }
    if tu_volt and not znp_volt:
        mismatches.append(f"В ЗнП не указано значение напряжения из ТУ ({tu_volt})")
    elif tu_volt and znp_volt and tu_volt != znp_volt:
        mismatches.append(f"Несовпадение напряжения: ТУ={tu_volt}, ЗнП={znp_volt}")

    # 3. Категория надёжности
    cat_re = r"\b(I{1,3}|1|2|3)\s*(?:-?[аяе]?\s*)?категори"
    cat_map = {"1": "I", "2": "II", "3": "III", "i": "I", "ii": "II", "iii": "III"}
    tu_cat = {cat_map.get(c.lower(), c.upper()) for c in re.findall(cat_re, tu_text, re.IGNORECASE)}
    znp_cat = {cat_map.get(c.lower(), c.upper()) for c in re.findall(cat_re, znp_text, re.IGNORECASE)}
    if tu_cat and not znp_cat:
        mismatches.append(f"В ЗнП не указана категория надёжности из ТУ ({tu_cat})")
    elif tu_cat and znp_cat and tu_cat != znp_cat:
        mismatches.append(f"Несовпадение категории надёжности: ТУ={tu_cat}, ЗнП={znp_cat}")

    # 4. Класс точности учёта
    acc_re = r"класс[ауеом]*\s+точност[иью]\s+(?:не\s+ниже\s+)?(\d+(?:[.,]\d+)?[sSсС]?)"
    tu_acc = {m.replace(",", ".").upper().replace("С", "S") for m in re.findall(acc_re, tu_text, re.IGNORECASE)}
    znp_acc = {m.replace(",", ".").upper().replace("С", "S") for m in re.findall(acc_re, znp_text, re.IGNORECASE)}
    if tu_acc and not znp_acc:
        mismatches.append(f"В ЗнП не указан класс точности из ТУ ({tu_acc})")
    elif tu_acc and znp_acc and tu_acc != znp_acc:
        mismatches.append(f"Несовпадение класса точности: ТУ={tu_acc}, ЗнП={znp_acc}")

    return {
        "params_match": len(mismatches) == 0,
        "mismatches": mismatches,
        "extracted": {
            "tu": {"power": list(tu_power), "voltage": list(tu_volt), "category": list(tu_cat), "accuracy": list(tu_acc)},
            "znp": {"power": list(znp_power), "voltage": list(znp_volt), "category": list(znp_cat), "accuracy": list(znp_acc)},
        },
    }


# ---------------------------------------------------------------------------
# ИНСТРУМЕНТ 4: Проверка покрытия перечня требований
# ---------------------------------------------------------------------------
def tool_check_list_coverage(tu_text: str, znp_text: str) -> dict[str, Any]:
    """Проверяет, подтверждены ли в ЗнП все элементы перечня требования ТУ.

    Перечень распознаётся по явным маркерам, вводной строке с двоеточием или
    inline-сегментам через ``;``. Обычный абзац с одной-двумя точками с запятой
    перечнем не считается.
    """
    items = _list_items(tu_text)
    if len(items) < 2:
        return {
            "is_list": False,
            "items_total": 0,
            "items_covered": 0,
            "coverage_ratio": 1.0,
            "covered": True,
            "uncovered_items": [],
            "items": [],
        }

    znp_canonical = _canonical_text(znp_text)
    znp_tokens = _content_tokens(znp_text)
    item_results: list[dict[str, Any]] = []
    uncovered: list[str] = []

    for index, item in enumerate(items, start=1):
        identifiers = _extract_identifiers(item)
        tokens = _content_tokens(item)
        if identifiers:
            covered = all(
                _identifier_present(identifier, znp_canonical)
                for identifier in identifiers
            )
        else:
            overlap = tokens & znp_tokens
            ratio = len(overlap) / max(len(tokens), 1)
            covered = bool(tokens) and ratio >= 0.65
        if not covered:
            uncovered.append(_shorten(item))
        item_results.append({
            "index": index,
            "text": _shorten(item),
            "covered": covered,
        })

    covered_count = sum(1 for item in item_results if item["covered"])
    total = len(item_results)
    return {
        "is_list": True,
        "items_total": total,
        "items_covered": covered_count,
        "coverage_ratio": round(covered_count / total, 3),
        "covered": covered_count == total,
        "uncovered_items": uncovered,
        "items": item_results,
    }


# ---------------------------------------------------------------------------
# ИНСТРУМЕНТ 5: Выбор атомарного доказательства из пункта ЗнП
# ---------------------------------------------------------------------------
def _evidence_fragments(candidate_text: str, max_chars: int) -> list[str]:
    starts = [0]
    for match in _EVIDENCE_BOUNDARY_RE.finditer(candidate_text):
        starts.append(match.end())
    starts = sorted({start for start in starts if start < len(candidate_text)})
    starts.append(len(candidate_text))

    fragments: list[str] = []
    for start, end in zip(starts, starts[1:]):
        fragment = candidate_text[start:end].strip()
        if not fragment:
            continue
        if len(fragment) <= max_chars:
            fragments.append(fragment)
            continue
        remaining = fragment
        while len(remaining) > max_chars:
            cut = remaining.rfind(" ", 0, max_chars)
            if cut < max_chars // 2:
                cut = max_chars
            piece = remaining[:cut].strip()
            if piece:
                fragments.append(piece)
            remaining = remaining[cut:].lstrip()
        if remaining.strip():
            fragments.append(remaining.strip())
    return fragments


def tool_pick_best_evidence(
    query: str,
    candidate_text: str,
    max_chars: int = _MAX_ATOMIC_QUOTE_CHARS,
) -> dict[str, Any]:
    """Выбирает короткий атомарный фрагмент кандидата, близкий к требованию."""
    text = candidate_text or ""
    if not text.strip():
        return {"quote": "", "score": 0.0, "paragraph_index": 0, "is_atomic": False}

    fragments = _evidence_fragments(text, max_chars)
    query_tokens = _content_tokens(query)
    query_identifiers = _extract_identifiers(query)
    query_numbers = {value.replace(",", ".") for value in _NUMBER_RE.findall(query)}

    best_fragment = fragments[0] if fragments else text[:max_chars].strip()
    best_score = -1.0
    best_index = 0
    for index, fragment in enumerate(fragments):
        fragment_tokens = _content_tokens(fragment)
        overlap = query_tokens & fragment_tokens
        token_score = len(overlap) / max(len(query_tokens), 1)

        fragment_identifiers = _extract_identifiers(fragment)
        identifier_score = (
            len(query_identifiers & fragment_identifiers) / len(query_identifiers)
            if query_identifiers else 0.0
        )
        fragment_numbers = {
            value.replace(",", ".") for value in _NUMBER_RE.findall(fragment)
        }
        number_score = (
            len(query_numbers & fragment_numbers) / len(query_numbers)
            if query_numbers else 0.0
        )
        score = 0.55 * token_score + 0.30 * identifier_score + 0.15 * number_score
        if 20 <= len(fragment) <= max_chars:
            score += 0.03
        elif len(fragment) < 20:
            score -= 0.05

        if score > best_score:
            best_score = score
            best_fragment = fragment
            best_index = index

    quote = best_fragment.strip()
    return {
        "quote": quote,
        "score": round(max(best_score, 0.0), 3),
        "paragraph_index": best_index,
        "is_atomic": _is_atomic_quote(quote, max_chars),
    }


# ---------------------------------------------------------------------------
# ИНСТРУМЕНТ 6: Валидатор точности доказательных цитат
# ---------------------------------------------------------------------------
def tool_validate_quotes(
    tu_full_text: str,
    tu_quote: str | None,
    znp_full_text: str | None,
    znp_quote: str | None,
) -> dict[str, Any]:
    """Проверяет подлинность и атомарность доказательных цитат."""
    tu_present = bool(tu_quote and tu_quote.strip())
    znp_present = bool(znp_quote and znp_quote.strip())

    tu_valid = False
    if tu_present:
        tu_valid = _normalize_quote_text(tu_quote) in _normalize_quote_text(tu_full_text)

    znp_valid = False
    if znp_present and znp_full_text:
        znp_valid = _normalize_quote_text(znp_quote) in _normalize_quote_text(znp_full_text)

    return {
        "is_valid": tu_valid and znp_valid,
        "tu_quote_valid": tu_valid,
        "znp_quote_valid": znp_valid,
        "tu_quote_chars": len((tu_quote or "").strip()),
        "znp_quote_chars": len((znp_quote or "").strip()),
        "znp_quote_present": znp_present,
        "znp_quote_atomic": _is_atomic_quote(znp_quote),
    }


def classify_requirement_category(text: str) -> RequirementCategory:
    """Классификатор категории требования ТУ по тексту."""
    lower = text.lower().replace("ё", "е")
    if any(k in lower for k in ("мощност", "квт", "мвт", "ква")):
        return RequirementCategory.power
    if any(k in lower for k in ("категори", "надежност", "резерв")):
        return RequirementCategory.reliability
    if any(k in lower for k in ("напряжен", "0,4 кв", "0.4 кв", "6 кв", "10 кв", "110 кв")):
        return RequirementCategory.voltage
    if any(k in lower for k in ("точк", "присоединен", "балансов", "ру-")):
        return RequirementCategory.connection_point
    if any(k in lower for k in ("учет", "счетчик", "аиис", "0.5s", "0,5s")):
        return RequirementCategory.metering
    if any(k in lower for k in ("релейн", "защит", "рза", "противоаварийн", "автоматик")):
        return RequirementCategory.protection
    if any(k in lower for k in ("трансформатор", "выключател", "кабел", "оборудован", "ячейк")):
        return RequirementCategory.equipment
    return RequirementCategory.other