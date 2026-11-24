"""Инструменты ИИ-агента для сверки требований ТУ и пунктов ЗнП."""
import math
import re
from decimal import Decimal
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


_NUMBER_VALUE_RE = (
    r"(?:\d{1,3}(?:\s\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
)
_UNIT_SRC = (
    r"(?:"
    r"г\s*\.?\s*[уУ]\s*\.?\s*т\.?\s*/\s*\(?\s*[кК][вВ][тТ]\s*[·*x×]\s*ч\s*\)?"
    r"|(?:[кКмМГг][бБ][иИ][тТ])\s*/\s*[сС]"
    r"|(?:[кКмМ][вВ][тТ]|[Мм][вВ][аА])\s*[·*x×]\s*ч"
    r"|(?:тыс\.?\s*)?(?:м3|м³|л)\s*/\s*(?:ч|с|мин)"
    r"|(?:[кКмМ]?[вВ][тТ])"
    r"|(?:[кКмМ]?[вВ][аА])"
    r"|(?:[кК][вВ])"
    r"|В"
    r"|(?:[кКмМ]?А)"
    r"|(?:[кКмМ]?[оО]м)"
    r"|(?:мкс|мс|мин|сут(?:ок|ки)?|дней|день|дн|ч|с)"
    r"|(?:[гГ][кК][аА][лЛ]|[кК][кК][аА][лЛ]|кал)"
    r"|(?:[кКмМ]?[гГ][цЦ])"
    r"|(?:мм2|мм²|см2|см²|м2|м²|м3|м³|мм|см|км|м)"
    r"|(?:шт|кг|т|л)"
    r"|%"
    r")"
)
_EXCLUDED_CONSTRAINT_UNITS = frozenset(
    {"квт", "мвт", "вт", "ква", "мва", "ва", "в", "кв"}
)
_COMPARISON_SRC = (
    r"не\s+(?:должн\w*\s+)?(?:быть\s+)?(?:более|менее|выше|ниже|превыша\w*)"
    r"|не\s+превыша\w*"
    r"|<=|>=|≤|≥"
    r"|более|менее|выше|ниже"
)
_RANGE_CONSTRAINT_RE = re.compile(
    r"(?<![а-яёa-z])(?i:от)\s*(?P<v1>"
    + _NUMBER_VALUE_RE
    + r")\s*(?:(?P<u1>"
    + _UNIT_SRC
    + r")\s*)?(?i:до)\s*(?P<v2>"
    + _NUMBER_VALUE_RE
    + r")\s*(?P<u2>"
    + _UNIT_SRC
    + r")(?![а-яёa-z0-9])"
)
_SINGLE_CONSTRAINT_RE = re.compile(
    r"(?P<op>(?i:" + _COMPARISON_SRC + r"))\s*(?P<value>"
    + _NUMBER_VALUE_RE
    + r")\s*(?P<unit>"
    + _UNIT_SRC
    + r")(?![а-яёa-z0-9])"
)
_TABLE_OP_VALUE_RE = re.compile(
    r"^(?P<op>(?i:" + _COMPARISON_SRC + r"))\s*"
    r"(?P<value>" + _NUMBER_VALUE_RE + r")"
    r"(?:\s*(?P<unit>" + _UNIT_SRC + r"))?$"
)
_TABLE_UNIT_RE = re.compile(r"^(?:" + _UNIT_SRC + r")$", re.IGNORECASE)
_TABLE_VALUE_RE = re.compile(r"^(?P<value>" + _NUMBER_VALUE_RE + r")$")
_TABLE_LABEL_OP_RE = re.compile(
    r"\(\s*(?P<op>не\s+(?:более|менее|выше|ниже))\s*\)",
    re.IGNORECASE,
)
_ACCURACY_PHRASE_RE = re.compile(r"класс\w*\s+точност\w*", re.IGNORECASE)
_ACCURACY_NUMBER_RE = re.compile(
    r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*([sSсС])?(?![\d])"
)
_ACCURACY_FORBIDDEN_UNIT_RE = re.compile(
    r"\s*(?:квт|мвт|ква|мва|месяц\w*|недел\w*|дней|день|сут\w*|"
    r"кв|вт|ва|мм|см|км|мс|мин|мес|год\w*|лет|раз|чел|руб|кг|"
    r"гц|ом|шт|дн|ч|с|г|т|л|м|в|а|%)(?![а-яёa-z0-9])",
    re.IGNORECASE,
)
_ACCURACY_CLASS_VALUES = frozenset(
    {"0.2", "0.2s", "0.25", "0.5", "0.5s", "0.6s", "1", "1s", "1.5", "2", "2.5"}
)
_CONTEXT_GENERIC_TOKENS = frozenset({
    "должен", "должна", "должно", "должны", "быть", "состав",
    "значен", "параме", "величи", "показа", "уровен", "требов",
    "необхо", "обеспе", "предус", "выполн", "работ", "электр",
    "энерг", "сетей", "сети", "сеть", "устрои", "устрой", "объект",
    "проект", "оборуд", "систем", "более", "менее", "выше", "ниже",
})
_CODE_WORD = (
    r"(?:гост(?:\s+р)?(?:\s+(?:мэк|исо|iec))?|тр\s+тс|снип|сп|сто|рд|со|фз|"
    r"санпин|птээп|птэ|пб|всн|пуэ|итс|внтп|пп\s+рф)"
)
_CODE_VALUE = (
    r"\d[\w./-]*"
    r"(?:\s*\(\s*\d+(?:\s*[–—-]\s*\d+)?\s*\)\s*[-\w./]*)?"
)
_NORMATIVE_CODE_RE = re.compile(
    r"(?<![а-яёa-z])" + _CODE_WORD + r"\s*(?:№\s*)?" + _CODE_VALUE,
    re.IGNORECASE,
)
_ORDER_CODE_RE = re.compile(
    r"(?<![а-яёa-z])(приказ\w*|постановлени\w*|распоряжени\w*|решение|"
    r"пп\s+рф)\b[^;\n]{0,80}?№\s*(\d[\w./-]*)",
    re.IGNORECASE,
)
_ORDER_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2}\.\d{1,2}\.\d{2,4})(?!\d)",
)
_LAW_NUMBER_RE = re.compile(r"(?<!\d)(\d+)\s*[-–]\s*фз\b", re.IGNORECASE)
_BARE_PUE_RE = re.compile(r"(?<![а-яёa-z])пуэ(?![а-яёa-z])", re.IGNORECASE)
_NO_NUMBER_RE = re.compile(r"№\s*(\d[\w./-]*)", re.IGNORECASE)
_NO_NUMBER_LIST_RE = re.compile(
    r"№\s*(\d[\w./-]*(?:\s*,\s*\d[\w./-]*)+)",
    re.IGNORECASE,
)
_NO_CLAUSE_PREFIX_RE = re.compile(
    r"(?:^|[\s(])(?:п\.?|пп\.?|пункт\w*|раздел\w*|подраздел\w*|"
    r"табл\w*|таблиц\w*|рис\.?|рисун\w*|форм\w*|позиц\w*|этап\w*|"
    r"приложени\w*|лист\w*|стр\.?|страниц\w*|дом\w*|д\.|корпус\w*|"
    r"корп\.|строени\w*|квартир\w*|кв\.|офис\w*|помещени\w*|"
    r"участ\w*|уч\.|модел\w*|марк\w*)\s*$",
    re.IGNORECASE,
)
_NORMATIVE_BEFORE_NO_RE = re.compile(
    r"(?:гост(?:\s+р)?(?:\s+(?:мэк|исо|iec))?|тр\s+тс|снип|сп|сто|рд|со|"
    r"пуэ|фз|санпин|итс|внтп|пп\s+рф|"
    r"приказ\w*|постановлени\w*|распоряжени\w*|закон\w*)\s*$",
    re.IGNORECASE,
)
_CODE_PREFIX_RE = re.compile(
    r"^(?P<prefix>гост(?:\s+р)?(?:\s+(?:мэк|исо|iec))?|тр\s+тс|снип|сп|сто|"
    r"рд|со|фз|санпин|птээп|птэ|пб|всн|пуэ|итс|внтп|пп\s+рф)\s*(?P<rest>.*)$",
    re.IGNORECASE,
)


def _normalize_number(raw: str) -> str:
    cleaned = raw.replace(",", ".").replace(" ", "").strip()
    try:
        number = float(cleaned)
    except ValueError:
        return cleaned
    return str(int(number)) if number.is_integer() else str(number)


def _canonical_unit(raw: str) -> str:
    unit = _normalize_space(raw).casefold()
    unit = re.sub(r"г\s*\.\s*у\s*\.\s*т\s*\.", "гут", unit)
    unit = re.sub(r"у\s*\.\s*т\s*\.", "ут", unit)
    unit = unit.replace("²", "2").replace("³", "3")
    unit = unit.replace("·", "*").replace("×", "*").replace("х", "*")
    unit = re.sub(r"\s+", "", unit)
    unit = unit.replace(".", "").replace("(", "").replace(")", "")
    return unit


def _comparison_kind(raw: str) -> str | None:
    text = raw.casefold().replace("ё", "е")
    if text in {"≤", "<="}:
        return "max"
    if text in {"≥", ">="}:
        return "min"
    if "превыш" in text:
        return "max"
    if "не менее" in text or "не ниже" in text:
        return "min"
    if "не более" in text or "не выше" in text:
        return "max"
    if text.startswith("менее") or text.startswith("ниже"):
        return "max"
    if text.startswith("более") or text.startswith("выше"):
        return "min"
    if "менее" in text or "ниже" in text:
        return "min"
    if "более" in text or "выше" in text:
        return "max"
    return None


def _constraint_context(text: str, start: int) -> tuple[str, set[str]]:
    prefix = text[max(0, start - 180):start]
    boundaries = [prefix.rfind("\n"), prefix.rfind(";")]
    sentence_boundaries = list(re.finditer(r"(?<=[.!?])\s+", prefix))
    if sentence_boundaries:
        boundaries.append(sentence_boundaries[-1].start())
    cut = max(boundaries)
    context_text = (prefix[cut + 1:] if cut >= 0 else prefix).strip()[-120:]
    tokens = {
        token
        for token in _content_tokens(context_text)
        if not token.isdigit() and token not in _CONTEXT_GENERIC_TOKENS
    }
    return _shorten(context_text, 120), tokens


def _table_nearest_unit(
    cells: list[str],
    index: int,
    unit_indices: list[int],
) -> str:
    if not unit_indices:
        return ""
    closest = min(unit_indices, key=lambda item: (abs(item - index), item < index))
    return cells[closest]


def _table_constraint_context(cells: list[str], index: int) -> tuple[str, set[str]]:
    candidates = [
        cell
        for cell_index, cell in enumerate(cells)
        if cell_index != index
        and len(cell) >= 4
        and not _TABLE_UNIT_RE.fullmatch(cell)
        and not _TABLE_VALUE_RE.fullmatch(cell)
        and not _TABLE_OP_VALUE_RE.fullmatch(cell)
    ]
    label = max(candidates, key=len, default="")
    context = _shorten(label, 120)
    tokens = {
        token
        for token in _content_tokens(label)
        if not token.isdigit() and token not in _CONTEXT_GENERIC_TOKENS
    }
    return context, tokens


def _extract_table_constraints(text: str) -> list[dict[str, Any]]:
    """Сравнения из строк таблиц DOCX вида `показатель | единица | ≤ 240`."""
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(
        comparison: str,
        value_raw: str,
        unit_raw: str,
        cells: list[str],
        index: int,
    ) -> None:
        unit = _canonical_unit(unit_raw)
        if not unit or unit in _EXCLUDED_CONSTRAINT_UNITS:
            return
        context, tokens = _table_constraint_context(cells, index)
        if any(token.startswith(("класс", "точнос")) for token in tokens):
            return
        value = _normalize_number(value_raw)
        key = (comparison, value, unit)
        if key in seen:
            return
        seen.add(key)
        found.append({
            "comparison": comparison,
            "value": value,
            "unit": unit,
            "unit_display": _normalize_space(unit_raw),
            "context": context,
            "context_tokens": tokens,
        })

    for raw_line in text.splitlines():
        if "|" not in raw_line:
            continue
        cells = [
            cell.strip()
            for cell in _normalize_space(raw_line).split("|")
            if cell.strip()
        ]
        if len(cells) < 3:
            continue
        unit_indices = [
            index for index, cell in enumerate(cells)
            if _TABLE_UNIT_RE.fullmatch(cell)
        ]

        for index, cell in enumerate(cells):
            match = _TABLE_OP_VALUE_RE.fullmatch(cell)
            if match is None:
                continue
            comparison = _comparison_kind(match.group("op"))
            if comparison is None:
                continue
            unit_raw = match.group("unit") or _table_nearest_unit(
                cells, index, unit_indices,
            )
            if unit_raw:
                add(comparison, match.group("value"), unit_raw, cells, index)

        for index, cell in enumerate(cells):
            label_match = _TABLE_LABEL_OP_RE.search(cell)
            if label_match is None:
                continue
            comparison = _comparison_kind(label_match.group("op"))
            if comparison is None:
                continue
            value_index = next(
                (
                    candidate
                    for candidate in range(index + 1, len(cells))
                    if candidate not in unit_indices
                    and _TABLE_VALUE_RE.fullmatch(cells[candidate])
                ),
                None,
            )
            if value_index is None:
                continue
            unit_raw = _table_nearest_unit(cells, index, unit_indices)
            if not unit_raw:
                continue
            value_match = _TABLE_VALUE_RE.fullmatch(cells[value_index])
            if value_match is None:
                continue
            add(
                comparison,
                value_match.group("value"),
                unit_raw,
                cells,
                index,
            )
    return found


def _extract_constraints(text: str) -> list[dict[str, Any]]:
    """Извлекает сравнения с единицами, кроме мощности/напряжения и точности."""
    clean = _normalize_space(text)
    if not clean:
        return []

    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(
        comparison: str,
        value: str,
        unit_raw: str,
        start: int,
    ) -> None:
        unit = _canonical_unit(unit_raw)
        if not unit or unit in _EXCLUDED_CONSTRAINT_UNITS:
            return
        context, tokens = _constraint_context(clean, start)
        if any(token.startswith(("класс", "точнос")) for token in tokens):
            return
        key = (comparison, value, unit)
        if key in seen:
            return
        seen.add(key)
        found.append({
            "comparison": comparison,
            "value": value,
            "unit": unit,
            "unit_display": _normalize_space(unit_raw),
            "context": context,
            "context_tokens": tokens,
        })

    for match in _RANGE_CONSTRAINT_RE.finditer(clean):
        first_unit = match.group("u1")
        second_unit = match.group("u2")
        if first_unit and second_unit and _canonical_unit(first_unit) != _canonical_unit(second_unit):
            continue
        add(
            "range",
            f"{_normalize_number(match.group('v1'))}-{_normalize_number(match.group('v2'))}",
            first_unit or second_unit,
            match.start(),
        )

    for match in _SINGLE_CONSTRAINT_RE.finditer(clean):
        kind = _comparison_kind(match.group("op"))
        if kind is None:
            continue
        add(
            kind,
            _normalize_number(match.group("value")),
            match.group("unit"),
            match.start(),
        )

    for item in _extract_table_constraints(text):
        key = (item["comparison"], item["value"], item["unit"])
        if key in seen:
            continue
        seen.add(key)
        found.append(item)
    return found


def _search_tokens(text: str) -> set[str]:
    """Лексические токены поиска без общеотраслевых слов-«шумов»."""
    return {
        token
        for token in _content_tokens(text)
        if token not in _CONTEXT_GENERIC_TOKENS
    }


def _extract_search_parameters(text: str) -> list[dict[str, Any]]:
    """Параметры для поиска: сравнения, мощность и напряжение."""
    clean = _normalize_space(text)
    parameters = list(_extract_constraints(text))
    seen = {
        (
            item["comparison"],
            item["value"],
            item["unit"],
            " ".join(sorted(item["context_tokens"])),
        )
        for item in parameters
    }

    def add(
        comparison: str,
        value: str,
        unit_raw: str,
        start: int,
    ) -> None:
        unit = _canonical_unit(unit_raw)
        if not unit:
            return
        context, tokens = _constraint_context(clean, start)
        key = (comparison, value, unit, " ".join(sorted(tokens)))
        if key in seen:
            return
        seen.add(key)
        parameters.append({
            "comparison": comparison,
            "value": value,
            "unit": unit,
            "unit_display": _normalize_space(unit_raw),
            "context": context,
            "context_tokens": tokens,
        })

    power_re = re.compile(
        r"(\d+(?:[.,]\d+)?)\s*(кВт|МВт|кВА|МВА)\b(?![·*x×]\s*ч)",
        re.IGNORECASE,
    )
    for match in power_re.finditer(clean):
        add(
            "value",
            _normalize_number(match.group(1)),
            match.group(2),
            match.start(),
        )

    voltage_re = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:([кК][вВ])|(В))\b")
    for match in voltage_re.finditer(clean):
        add(
            "value",
            _normalize_number(match.group(1)),
            match.group(2) or match.group(3),
            match.start(),
        )

    return parameters


def _contexts_overlap(left: set[str], right: set[str]) -> bool:
    return bool(left and right and left & right)


def _public_constraint(constraint: dict[str, Any]) -> dict[str, Any]:
    return {
        "comparison": constraint["comparison"],
        "value": constraint["value"],
        "unit": constraint["unit_display"],
        "context": constraint["context"],
    }


def _constraint_values_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left["comparison"] == right["comparison"]
        and left["value"] == right["value"]
    )


def _constraint_text(constraint: dict[str, Any]) -> str:
    if constraint["comparison"] == "range":
        return (
            f"от {constraint['value'].replace('-', ' до ')} "
            f"{constraint['unit_display']}"
        )
    label = "не более" if constraint["comparison"] == "max" else "не менее"
    return f"{label} {constraint['value']} {constraint['unit_display']}"


def _constraint_missing_message(constraint: dict[str, Any]) -> str:
    context = f" «{constraint['context']}»" if constraint["context"] else ""
    return f"В ЗнП не найден параметр{context}: ТУ {_constraint_text(constraint)}"


def _constraint_mismatch_message(
    requirement: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> str:
    context = f" «{requirement['context']}»" if requirement["context"] else ""
    found = "; ".join(_constraint_text(item) for item in candidates[:2])
    return (
        f"Несовпадение параметра{context}: "
        f"ТУ {_constraint_text(requirement)}; ЗнП {found}"
    )


def _extract_accuracy_classes(text: str) -> set[str]:
    """Классы точности из окна после фразы «класс(ы) точности»."""
    clean = _normalize_space(text)
    classes: set[str] = set()
    for phrase in _ACCURACY_PHRASE_RE.finditer(clean):
        window = clean[phrase.end():phrase.end() + 240]
        sentence_end = re.search(r"[.!?]\s+(?=[А-ЯЁA-Z«\"(])", window)
        if sentence_end:
            window = window[:sentence_end.start()]
        for match in _ACCURACY_NUMBER_RE.finditer(window):
            tail = window[match.end():match.end() + 12]
            if _ACCURACY_FORBIDDEN_UNIT_RE.match(tail):
                continue
            suffix = (match.group(2) or "").casefold().replace("с", "s")
            value = _normalize_number(match.group(1))
            candidate = f"{value}{'S' if suffix == 's' else ''}"
            if candidate.casefold() in _ACCURACY_CLASS_VALUES:
                classes.add(candidate)
    return classes


def _normalize_code(raw: str) -> str:
    code = _normalize_space(raw).casefold()
    code = re.sub(r"\s*[-–—]\s*", "-", code)
    code = re.sub(r"\s*№\s*", "№", code)
    code = re.sub(r"(?<=[0-9a-zа-я])\.(?=\s*от\b|\s*$)", "", code)
    code = re.sub(r"\s*([./])\s*", r"\1", code)
    code = re.sub(r"\s+", " ", code)
    code = re.sub(
        r"^(гост(?:\s+р)?(?:\s+(?:мэк|исо|iec))?|тр\s+тс|снип|сп|сто|рд|со|"
        r"фз|санпин|птээп|птэ|пб|всн|пуэ|итс|внтп|пп\s+рф)\s*",
        r"\1 ",
        code,
    )
    return code.strip(" .,;:")


def _normalize_code_number(raw: str) -> str:
    value = raw.strip(" .,;:")
    value = value.replace("№", "")
    value = re.sub(r"[./–—]", "-", value)
    value = re.sub(r"[^0-9a-zа-я-]+", "", value)
    return re.sub(r"-{2,}", "-", value).strip("-")


def _normalize_code_date(raw: str) -> str:
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", raw or "")
    if match is None:
        return ""
    day, month, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000 if year < 70 else 1900
    return f"{day:02d}.{month:02d}.{year:04d}"


def _code_parts(code: str) -> dict[str, str | bool]:
    """Разбирает обозначение на тип, номер и дату для мягкого сравнения."""
    normalized = _normalize_code(code)
    order = re.match(
        r"^(приказ|постановление|распоряжение|решение)\s*№\s*"
        r"(?P<number>[^\s]+?)(?:\s+от\s+(?P<date>\d{1,2}\.\d{1,2}\.\d{2,4}))?$",
        normalized,
    )
    if order:
        return {
            "prefix": order.group(1),
            "number": _normalize_code_number(order.group("number")),
            "date": _normalize_code_date(order.group("date") or ""),
            "is_order": True,
        }

    prefix_match = _CODE_PREFIX_RE.match(normalized)
    if prefix_match:
        return {
            "prefix": prefix_match.group("prefix").casefold(),
            "number": _normalize_code_number(prefix_match.group("rest")),
            "date": "",
            "is_order": False,
        }

    if normalized.startswith("№"):
        rest = normalized[1:].strip()
        date_match = _ORDER_DATE_RE.search(rest)
        number = rest[: date_match.start()] if date_match else rest
        return {
            "prefix": "№",
            "number": _normalize_code_number(number),
            "date": _normalize_code_date(date_match.group(0) if date_match else ""),
            "is_order": False,
        }

    return {
        "prefix": "",
        "number": _normalize_code_number(normalized),
        "date": "",
        "is_order": False,
    }


def _code_key(code: str) -> tuple[str, str, str]:
    parts = _code_parts(code)
    return parts["prefix"], parts["number"], parts["date"]


def _codes_match(left: str, right: str) -> bool:
    """Сравнивает коды, допуская сокращённую запись без типа/даты."""
    left_parts = _code_parts(left)
    right_parts = _code_parts(right)
    left_prefix = left_parts["prefix"]
    right_prefix = right_parts["prefix"]
    if left_prefix != "№" and right_prefix != "№" and left_prefix != right_prefix:
        return False
    if left_parts["number"] != right_parts["number"]:
        return False
    if (
        left_parts["is_order"]
        and right_parts["is_order"]
        and left_parts["date"]
        and right_parts["date"]
    ):
        return left_parts["date"] == right_parts["date"]
    return True


def _extract_normative_codes(text: str) -> set[str]:
    clean = _normalize_space(text)
    codes: set[str] = set()
    occupied: list[tuple[int, int]] = []

    for match in _ORDER_CODE_RE.finditer(clean):
        raw_type = match.group(1).casefold()
        if raw_type.startswith("приказ"):
            order_type = "приказ"
        elif raw_type.startswith(("постановлени", "пп")):
            order_type = "постановление"
        elif raw_type.startswith("распоряжени"):
            order_type = "распоряжение"
        else:
            order_type = "решение"
        date_match = _ORDER_DATE_RE.search(match.group(0))
        suffix = f" от {date_match.group(1)}" if date_match else ""
        codes.add(_normalize_code(f"{order_type} №{match.group(2)}{suffix}"))
        occupied.append(match.span())

    for match in _NORMATIVE_CODE_RE.finditer(clean):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        codes.add(_normalize_code(match.group(0)))
        occupied.append(match.span())

    for match in _LAW_NUMBER_RE.finditer(clean):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        codes.add(_normalize_code(f"№{match.group(1)}-фз"))
        occupied.append(match.span())

    if _BARE_PUE_RE.search(clean):
        codes.add("пуэ")

    for match in _NO_NUMBER_LIST_RE.finditer(clean):
        before = clean[max(0, match.start() - 40):match.start()]
        if not (
            _NORMATIVE_BEFORE_NO_RE.search(before)
            or _ORDER_DATE_RE.search(before)
        ):
            continue
        for part in match.group(1).split(","):
            number = part.strip()
            if number:
                codes.add(_normalize_code(f"№{number}"))
        occupied.append(match.span())

    for match in _NO_NUMBER_RE.finditer(clean):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        before = clean[max(0, match.start() - 40):match.start()]
        if _NO_CLAUSE_PREFIX_RE.search(before):
            continue
        # Одиночный «№ 12» в «ячейка № 12» — это не нормативный документ.
        if not (
            _NORMATIVE_BEFORE_NO_RE.search(before)
            or _ORDER_DATE_RE.search(before)
        ):
            continue
        codes.add(_normalize_code(match.group(0)))

    return codes


def _shorten(text: str, limit: int = 160) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _is_atomic_quote(quote: str | None, max_chars: int = _MAX_ATOMIC_QUOTE_CHARS,
                     *, verified_source_text: str | None = None) -> bool:
    """A quote must be one short fragment, not a whole glued list."""
    text = (quote or "").strip()
    if not text or len(text) > max_chars:
        return False
    if text.count("\n") > 1:
        if not verified_source_text or _normalize_quote_text(text) not in _normalize_quote_text(verified_source_text):
            return False
        # Layout wraps may continue one statement, but paragraph boundaries
        # and separate sentences/items are not permission to glue obligations.
        if re.search(r"\n\s*\n|[.!?;]\s*\n\s*[А-ЯЁа-яёA-Za-z]", text):
            return False
    return len(_ATOMIC_MARKER_RE.findall(text)) < 2


def _verified_quote_source(quote, sources, fragments_by_id, document_id):
    """Resolve a quote to one exact server-side AtomSource span and owner."""
    if not document_id or not fragments_by_id or not quote:
        return None
    verified = {}
    for source in sources or ():
        if not isinstance(source, dict):
            continue
        fragment = fragments_by_id.get(source.get("fragment_id"))
        if fragment is None:
            continue
        if isinstance(fragment, dict):
            owner, raw, page = fragment.get("document_id"), fragment.get("text"), fragment.get("page")
            identity = fragment.get("id")
        else:
            owner, raw, page = fragment.document_id, fragment.text, fragment.page
            identity = fragment.id
        start, end, text = source.get("start_offset"), source.get("end_offset"), source.get("text")
        if identity != source.get("fragment_id") or owner != document_id or not isinstance(raw, str) or not isinstance(text, str):
            continue
        if source.get("document_id", document_id) != document_id:
            continue
        if page is not None and source.get("page") != page:
            continue
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(raw):
            continue
        if raw[start:end] != text:
            continue
        if _normalize_quote_text(quote) in _normalize_quote_text(text):
            verified[(source["fragment_id"], start, end)] = text
    if len(verified) != 1:
        return None
    return next(iter(verified.values()))


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
    query_tokens = _search_tokens(norm_query)
    semantic_available = bool(
        query_embedding
        and clause_embeddings is not None
        and len(clause_embeddings) == len(znp_clauses)
    )
    if not query_tokens and not semantic_available:
        return []

    query_constraints = _extract_search_parameters(_strip_clause_number(query))

    scored: list[dict[str, Any]] = []
    for idx, clause in enumerate(znp_clauses):
        norm_clause = _strip_clause_number(clause.text).lower().replace("ё", "е")
        clause_tokens = _search_tokens(norm_clause)
        overlap = query_tokens & clause_tokens

        clause_constraints = _extract_search_parameters(
            _strip_clause_number(clause.text)
        )
        constraint_support = False
        constraint_conflict = False
        for required in query_constraints:
            candidates = [
                candidate
                for candidate in clause_constraints
                if candidate["unit"] == required["unit"]
            ]
            if required["context_tokens"]:
                candidates = [
                    candidate
                    for candidate in candidates
                    if _contexts_overlap(
                        required["context_tokens"], candidate["context_tokens"]
                    )
                ]
            for candidate in candidates:
                if _constraint_values_match(required, candidate):
                    constraint_support = True
                else:
                    constraint_conflict = True

        support_bonus = 0.0
        if constraint_conflict:
            support_bonus = -0.08
        elif constraint_support:
            support_bonus = 0.07

        lexical_score = max(
            0.0,
            min(
                (len(overlap) / max(len(query_tokens), 1)) + support_bonus,
                1.0,
            ),
        )
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
            "constraint_support": constraint_support,
            "constraint_conflict": constraint_conflict,
            "constraint_bonus": round(support_bonus, 3),
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
def _verification_table_unit(cell: str) -> bool:
    return bool(_TABLE_UNIT_RE.fullmatch(cell)
                or re.fullmatch(r"лет|год(?:а|ов)?|о\.?\s*е\.?", cell, re.I))


def _verification_text(text: str) -> str:
    """Read a separated table unit/value without altering source evidence."""
    units = {"mw": "МВт", "kw": "кВт", "mva": "МВА", "kva": "кВА", "kv": "кВ"}
    text = re.sub(r"(?<=\d)\s*(MW|kW|MVA|kVA|kV)\b",
                  lambda m: " " + units[m.group(1).casefold()], text, flags=re.I)
    extra = []
    for line in text.splitlines():
        cells = [c.strip() for c in re.split(r"\||\s+[—–]\s+", line)]
        if len(cells) < 3:
            continue
        units = [i for i, c in enumerate(cells) if _verification_table_unit(c)]
        labels = [i for i, c in enumerate(cells)
                  if re.search(r"[а-яёa-z]", c, re.I) and i not in units]
        if not units or not labels:
            continue
        for i, cell in enumerate(cells):
            if i <= labels[0]:
                continue
            interval = _VERIFICATION_RANGE_RE.fullmatch(_normalize_typography(cell))
            scalar = re.fullmatch(
                r"(?:(?i:" + _COMPARISON_SRC + r")\s*)?[+-]?" + _NUMBER_VALUE_RE
                + r"(?:\s*±\s*" + _NUMBER_VALUE_RE + r")?", cell,
            )
            if not scalar and not (interval and interval.group("unit") is None):
                continue
            unit = _table_nearest_unit(cells, i, units)
            if re.fullmatch(r"о\.?\s*е\.?", unit, re.I):
                unit = "о.е."
            label = cells[labels[0]]
            label_op = _TABLE_LABEL_OP_RE.search(label)
            if label_op and not interval and not re.match(r"(?i:" + _COMPARISON_SRC + r")", cell):
                label = _TABLE_LABEL_OP_RE.sub("", label).strip()
                cell = f"{label_op.group('op')} {cell}"
            extra.append(f"{label} {cell} {unit}")
    return text + "\n" + "\n".join(extra)


_SIGNED_NUMBER = r"[+-]?\d+(?:[.,]\d+)?"
_EQUIPMENT_DESIGNATION_RE = re.compile(
    r"(?<!\w)[А-ЯЁA-Z]{2,8}[-–]\d+(?:[-/]\d+[А-ЯЁA-Z]?\d*)*(?!\w)", re.I,
)
_EQUIPMENT_TYPE_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*[.)]?\s*(?:\|\s*)?)?"
    r"(?P<field>тип|марка|модель|обозначение)\s+(?:генерирующ\w*\s+)?"
    r"(?P<object>установк\w*|оборудован\w*|трансформатор\w*)"
    r"[\s|:—–-]*", re.I,
)


def equipment_type_field(text: str) -> tuple[str, str, str] | None:
    """Only an explicit, unambiguous equipment-type field, not an object mention."""
    fields = set()
    for line in text.splitlines():
        for match in _EQUIPMENT_TYPE_RE.finditer(line):
            designation = _EQUIPMENT_DESIGNATION_RE.match(line, match.end())
            if designation is None:
                continue
            # An alternative, negation, qualification or another object's
            # prefix is not an unambiguous single-valued type field.
            if line[designation.end():].strip(" .;|"):
                continue
            subject = match.group("object").casefold()
            subject = next(prefix for prefix in ("установк", "оборудован", "трансформатор")
                           if subject.startswith(prefix))
            fields.add((match.group("field").casefold(), subject,
                        _normalize_typography(designation.group()).casefold()))
    return next(iter(fields)) if len(fields) == 1 else None


def equipment_type_comparison(left: str, right: str) -> str:
    a, b = equipment_type_field(left), equipment_type_field(right)
    if not a or not b or a[:2] != b[:2]:
        return "unknown"
    return "match" if a[2] == b[2] else "mismatch"


_TOLERANCE_RE = re.compile(
    r"(?<![\w.,])(?P<center>" + _SIGNED_NUMBER + r")\s*±\s*"
    r"(?P<tolerance>" + _NUMBER_VALUE_RE + r")\s*(?P<unit>" + _UNIT_SRC
    + r")(?![а-яёa-z0-9])"
)
_YEARS_RE = re.compile(
    r"(?P<op>(?i:" + _COMPARISON_SRC + r"))?\s*(?<![\w.,])"
    r"(?P<value>" + _NUMBER_VALUE_RE + r")\s*(?:лет|год(?:а|ов)?)(?!\w)", re.I,
)
_VERIFICATION_RANGE_RE = re.compile(
    r"(?<![\w.])(?:от\s*)?(?P<lower>" + _SIGNED_NUMBER + r")"
    r"\s*(?:до|\.{2,}|…|[–—-])\s*(?P<upper>" + _SIGNED_NUMBER + r")"
    r"(?:\s*(?P<unit>о\.?\s*е\.?|" + _UNIT_SRC + r")(?![а-яёa-z0-9]))?(?![\d.,])",
    re.I,
)
_VERIFICATION_QUANTITY_RE = re.compile(
    r"(?<![\w.,])(?P<value>" + _SIGNED_NUMBER + r")\s*(?P<unit>"
    + _UNIT_SRC + r")(?![а-яёa-z0-9])"
)
_PARAMETER_OBJECT_RE = re.compile(
    r"трансформатор\w*|электростанци\w*|станци\w*|генератор\w*|\b(?:ГЭС|ГРЭС|ТЭЦ)\b", re.I,
)


def _verification_ranges(text: str) -> list[dict[str, str]]:
    found = []
    for line in _normalize_typography(text).splitlines():
        line = _NORMATIVE_CODE_RE.sub(" ", line)
        for match in _VERIFICATION_RANGE_RE.finditer(line):
            unit = match.group("unit")
            if not unit and "|" in line and any(
                _verification_table_unit(cell.strip()) for cell in line.split("|")
            ):
                # The normalized row already carries its unit. Do not add a
                # second, dimensionless copy of the same table interval.
                continue
            if not unit and re.search(r"(?<!\w)о\.?\s*е\.?(?!\w)", line, re.I):
                unit = "о.е."
            # Bare dates/identifiers are not engineering ranges.
            if not unit and not re.search(r"диапазон|коэффициент|предел", line, re.I):
                continue
            context, _ = _constraint_context(line, match.start())
            found.append(dict(lower=_normalize_number(match.group("lower")),
                              upper=_normalize_number(match.group("upper")),
                              unit=("о.е." if unit and re.fullmatch(r"о\.?\s*е\.?", unit, re.I)
                                    else _canonical_unit(unit or "1")), context=context))
    return found


def _verification_quantities(text: str) -> list[dict[str, str | None]]:
    found = []
    text = _ORDER_DATE_RE.sub(" ", _NORMATIVE_CODE_RE.sub(" ", _normalize_typography(text)))
    text = _TOLERANCE_RE.sub(lambda m: " " * len(m.group()), text)
    for match in _VERIFICATION_QUANTITY_RE.finditer(text):
        context, _ = _constraint_context(text, match.start())
        objects = list(_PARAMETER_OBJECT_RE.finditer(context))
        owner = None
        if objects:
            name = objects[-1].group().casefold()
            owner = "station"
            if name.startswith("трансформатор"):
                owner = "transformer"
            elif name.startswith("генератор"):
                owner = "generator"
        found.append(dict(value=_normalize_number(match.group("value")),
                          unit=_canonical_unit(match.group("unit")), object=owner))
    # An address number is an explicit identifier, not an unparsed physical
    # quantity. Compare it separately instead of certifying an empty result.
    for match in re.finditer(r"\b(?:д\.|дом)\s*(\d+[а-яa-z]?)(?!\w)", text, re.I):
        found.append(dict(value=match.group(1).casefold(), unit="house_number", object=None))
    return found


def _verification_tolerances(text: str) -> list[dict[str, str]]:
    found = []
    text = _normalize_typography(text)
    for m in _TOLERANCE_RE.finditer(text):
        center = Decimal(m.group('center').replace(',', '.'))
        tolerance = Decimal(m.group('tolerance').replace(',', '.').replace(' ', ''))
        context, _ = _constraint_context(text, m.start())
        found.append(dict(lower=_normalize_number(str(center-tolerance)),
                          upper=_normalize_number(str(center+tolerance)),
                          unit=_canonical_unit(m.group('unit')), context=context))
    return found


def _verification_lifetimes(text: str) -> list[dict[str, str]]:
    found = []
    for m in _YEARS_RE.finditer(text):
        context, _ = _constraint_context(text, m.start())
        scope = 'unknown'
        if re.search(r'срок\w*\s+служб|ресурс', context, re.I):
            scope = 'equipment_life'
        elif re.search(r'срок\w*\s+действ|действи\w*\s+документ', context, re.I):
            scope = 'document_validity'
        op = (m.group('op') or '').casefold().strip()
        comparison = _comparison_kind(op) if op else 'value'
        if op in {'более', 'выше'}:
            comparison = 'gt'
        elif op in {'менее', 'ниже'}:
            comparison = 'lt'
        item = dict(value=_normalize_number(m.group('value')), unit='year', scope=scope,
                    comparison=comparison, context=context)
        if item not in found:
            found.append(item)
    return found


def _parameter_context_key(context: str) -> tuple[frozenset[str], frozenset[str]]:
    # Keep the subject and parameter label, not just one shared generic word.
    # Retain explicit object identifiers (e.g. generator G-1 vs G-2).
    context = re.sub(r"^\s*\d+(?:\.\d+)*\s*\|", "", context)
    normalized = _normalize_typography(context).casefold()
    identifiers = frozenset(re.findall(
        r"(?<!\w)[а-яa-z]{1,8}-\d+(?:[-/]\d+[а-яa-z]?\d*)*(?!\w)"
        r"|(?<!\w)\d+(?!\w)", normalized
    ))
    words = frozenset(t for t in _content_tokens(context)
                      if not t.isdigit() and t not in {
                          'должен', 'должна', 'должно', 'должны', 'быть',
                          'более', 'менее', 'выше', 'ниже', 'значен', 'параме',
                      })
    return words, identifiers


def _same_parameter_context(left, right):
    a = _parameter_context_key(left.get('context', ''))
    b = _parameter_context_key(right.get('context', ''))
    return bool(a[0]) and a == b


def _same_interval(left, right):
    return (_same_parameter_context(left, right)
            and all(left[k] == right[k] for k in ('unit', 'lower', 'upper')))


def _mask_intervals(text: str) -> str:
    """Keep bounds out of the legacy scalar power/voltage/quantity check."""
    def mask(match):
        return ' ' * len(match.group())
    text = _TOLERANCE_RE.sub(mask, _normalize_typography(text))
    return _VERIFICATION_RANGE_RE.sub(
        lambda m: mask(m) if m.group('unit') else m.group(), text
    )


def _lifetime_interval(item):
    value = Decimal(item['value'])
    kind = item['comparison']
    if kind == 'value':
        return value, True, value, True
    if kind in {'min', 'gt'}:
        return value, kind == 'min', Decimal('Infinity'), False
    if kind in {'max', 'lt'}:
        return Decimal('-Infinity'), False, value, kind == 'max'
    return None


def _lifetime_relation(required, candidate):
    a = _lifetime_interval(required)
    b = _lifetime_interval(candidate)
    if a is None or b is None:
        return 'unknown'
    al, alc, au, auc = a
    bl, blc, bu, buc = b
    lower_inside = bl > al or (bl == al and (alc or not blc))
    upper_inside = bu < au or (bu == au and (auc or not buc))
    if lower_inside and upper_inside:
        return 'match'
    if (bu < al or bl > au or (bu == al and not (buc and alc))
            or (bl == au and not (blc and auc))):
        return 'mismatch'
    return 'unknown'


def tool_verify_engineering_params(tu_text: str, znp_text: str) -> dict[str, Any]:
    """Сверяет числовые, единичные и нормативные параметры между ТУ и ЗнП."""
    mismatches: list[str] = []
    mismatch_types: list[str] = []
    tu_text = _verification_text(tu_text)
    znp_text = _verification_text(znp_text)
    tu_ranges = _verification_ranges(tu_text)
    znp_ranges = _verification_ranges(znp_text)
    tu_tolerances = _verification_tolerances(tu_text)
    znp_tolerances = _verification_tolerances(znp_text)
    tu_lifetimes = _verification_lifetimes(tu_text)
    znp_lifetimes = _verification_lifetimes(znp_text)
    type_status = equipment_type_comparison(tu_text, znp_text)
    if type_status == "mismatch":
        mismatches.append("Различаются явно указанные типы оборудования")
        mismatch_types.append("mismatch")
    # Intervals are checked once below. Their endpoints are not scalar values.
    tu_scalar_text = _mask_intervals(tu_text)
    znp_scalar_text = _mask_intervals(znp_text)

    # 1. Мощность (кВт, МВт, кВА, МВА)
    power_re = r"(\d+(?:[.,]\d+)?)\s*(кВт|МВт|кВА|МВА)\b(?!\s*[·*x×]\s*ч)"
    tu_power = {
        (_normalize_number(m[0]), m[1].lower())
        for m in re.findall(power_re, tu_scalar_text, re.IGNORECASE)
    }
    znp_power = {
        (_normalize_number(m[0]), m[1].lower())
        for m in re.findall(power_re, znp_scalar_text, re.IGNORECASE)
    }
    shared_power_re = (
        r"\b(?:с|от)\s+(\d+(?:[.,]\d+)?)\s+до\s+"
        r"\d+(?:[.,]\d+)?\s*(кВт|МВт|кВА|МВА)\b(?!\s*[·*x×]\s*ч)"
    )
    for value, unit in re.findall(shared_power_re, tu_scalar_text, re.IGNORECASE):
        tu_power.add((_normalize_number(value), unit.lower()))
    for value, unit in re.findall(shared_power_re, znp_scalar_text, re.IGNORECASE):
        znp_power.add((_normalize_number(value), unit.lower()))
    if tu_power and znp_power and tu_power != znp_power:
        mismatches.append(f"Несовпадение мощности: ТУ={tu_power}, ЗнП={znp_power}")
        power_range = any(r["unit"] in {"квт", "мвт", "ква", "мва"} for r in tu_ranges)
        mismatch_types.append("range_scope_unknown" if power_range else "mismatch")
    elif tu_power and not znp_power:
        mismatches.append(f"В ЗнП не указано значение мощности из ТУ ({tu_power})")
        mismatch_types.append("missing")

    # 2. Класс напряжения: кВ регистронезависимо, а одиночная 'В' — только заглавная (не предлог 'в')
    volt_re = r"(\d+(?:[.,]\d+)?)\s*(?:([кК][вВ])|(В))\b"
    tu_volt = {
        (_normalize_number(m[0]), "кв" if m[1] else "в")
        for m in re.findall(volt_re, tu_scalar_text)
    }
    znp_volt = {
        (_normalize_number(m[0]), "кв" if m[1] else "в")
        for m in re.findall(volt_re, znp_scalar_text)
    }
    if tu_volt and not znp_volt:
        mismatches.append(f"В ЗнП не указано значение напряжения из ТУ ({tu_volt})")
        mismatch_types.append("missing")
    elif tu_volt and znp_volt and tu_volt != znp_volt:
        mismatches.append(f"Несовпадение напряжения: ТУ={tu_volt}, ЗнП={znp_volt}")
        mismatch_types.append("mismatch")

    # 3. Категория надёжности
    cat_re = r"\b(I{1,3}|1|2|3)\s*(?:-?[аяе]?\s*)?категори"
    cat_map = {"1": "I", "2": "II", "3": "III", "i": "I", "ii": "II", "iii": "III"}
    tu_cat = {cat_map.get(c.lower(), c.upper()) for c in re.findall(cat_re, tu_text, re.IGNORECASE)}
    znp_cat = {cat_map.get(c.lower(), c.upper()) for c in re.findall(cat_re, znp_text, re.IGNORECASE)}
    if tu_cat and not znp_cat:
        mismatches.append(f"В ЗнП не указана категория надёжности из ТУ ({tu_cat})")
        mismatch_types.append("missing")
    elif tu_cat and znp_cat and tu_cat != znp_cat:
        mismatches.append(f"Несовпадение категории надёжности: ТУ={tu_cat}, ЗнП={znp_cat}")
        mismatch_types.append("mismatch")

    # 4. Класс точности: значения берутся из окна после фразы, включая списки ТТ/ТН.
    tu_acc = _extract_accuracy_classes(tu_text)
    znp_acc = _extract_accuracy_classes(znp_text)
    if tu_acc and not znp_acc:
        mismatches.append(f"В ЗнП не указан класс точности из ТУ ({tu_acc})")
        mismatch_types.append("missing")
    elif tu_acc and znp_acc and tu_acc != znp_acc:
        mismatches.append(f"Несовпадение класса точности: ТУ={tu_acc}, ЗнП={znp_acc}")
        mismatch_types.append("mismatch")

    # 5. Прочие сравнения с единицами (время, проценты, сопротивление, расход и т.п.).
    tu_constraints = _extract_constraints(tu_text)
    znp_constraints = _extract_constraints(znp_text)
    for requirement in tu_constraints:
        if requirement['comparison'] == 'range' and any(
            interval['unit'] == requirement['unit']
            and f"{interval['lower']}-{interval['upper']}" == requirement['value']
            for interval in tu_ranges
        ):
            # Explicit ranges are compared with scope and bounds below.
            continue
        same_unit = [
            candidate
            for candidate in znp_constraints
            if candidate["unit"] == requirement["unit"]
        ]
        contextual = [
            candidate
            for candidate in same_unit
            if _contexts_overlap(
                requirement["context_tokens"], candidate["context_tokens"]
            )
        ]
        if not contextual and not requirement["context_tokens"]:
            contextual = same_unit
        if not contextual:
            mismatches.append(_constraint_missing_message(requirement))
            mismatch_types.append("missing")
        elif not any(
            _constraint_values_match(requirement, candidate)
            for candidate in contextual
        ):
            mismatches.append(
                _constraint_mismatch_message(requirement, contextual)
            )
            mismatch_types.append("range_scope_unknown" if requirement["comparison"] == "range" else "mismatch")

    # 6. Нормативные коды: каждый код из ТУ должен присутствовать в ЗнП.
    # Точки и дефисы в обозначениях взаимозаменяемы (СТО ...044.2010 /
    # СТО ...044-2010), поэтому сравниваем нормализованные ключи.
    tu_codes = _extract_normative_codes(tu_text)
    znp_codes = _extract_normative_codes(znp_text)
    for code in sorted(tu_codes):
        if not any(_codes_match(code, candidate) for candidate in znp_codes):
            mismatches.append(f"В ЗнП не найден нормативный код: {code}")
            mismatch_types.append("missing")

    tu_quantities = _verification_quantities(tu_scalar_text)
    znp_quantities = _verification_quantities(znp_scalar_text)
    range_uncertain = False
    for required in tu_ranges:
        candidates = [c for c in znp_ranges + znp_tolerances
                      if c['unit'] == required['unit'] and _same_parameter_context(required, c)]
        if not candidates or not all(_same_interval(required, c) for c in candidates):
            range_uncertain = True
            break
    if range_uncertain:
        # A wider capability and a violated allowable interval are not the
        # same engineering claim. Retain both bounds for expert review.
        mismatches.append("Границы диапазона различаются или не подтверждены; требуется проверка смысла ограничения")
        mismatch_types.append("range_scope_unknown")
    object_uncertain = False
    if tu_power and znp_power and not ({u for _, u in tu_power} & {u for _, u in znp_power}):
        object_uncertain = True  # Active and apparent power are not comparable.
    for required in tu_quantities:
        candidates = [q for q in znp_quantities if q["unit"] == required["unit"]]
        if required["object"] and candidates and all(
            q["object"] and q["object"] != required["object"] for q in candidates
        ):
            object_uncertain = True
        own_candidates = [q for q in candidates if q["object"] == required["object"]]
        if required["object"] and own_candidates and not tu_ranges and not any(
            q["value"] == required["value"] for q in own_candidates
        ):
            mismatches.append("Различается параметр конкретного объекта: " + str(required["object"]))
            mismatch_types.append("mismatch")
        # Plain quantities outside the old power/voltage/inequality checks
        # still need a comparable value; absence is not equality.
        if (required["unit"] not in _EXCLUDED_CONSTRAINT_UNITS
                and not any(c["unit"] == required["unit"] for c in tu_constraints)
                and not any(r["unit"] == required["unit"] for r in tu_ranges)):
            if not candidates:
                mismatches.append("Не найден параметр с сопоставимой единицей измерения: " + str(required["unit"]))
                mismatch_types.append("missing")
            elif not any(q["value"] == required["value"] for q in candidates):
                mismatches.append("Различаются значения параметра: " + str(required["unit"]))
                mismatch_types.append("mismatch")
    if object_uncertain:
        mismatches.append("Числовые параметры относятся к разным объектам")
        mismatch_types.append("object_scope_unknown")
    for required in tu_tolerances:
        candidates = [c for c in znp_tolerances + znp_ranges
                      if c['unit'] == required['unit'] and _same_parameter_context(required, c)]
        if candidates and all(_same_interval(required, c) for c in candidates):
            continue
        kind = 'tolerance_scope_unknown'
        if candidates and all(float(c['upper']) < float(required['lower'])
                              or float(c['lower']) > float(required['upper']) for c in candidates):
            kind = 'mismatch'
        mismatches.append('Абсолютный допуск не подтверждён или требует проверки направления ограничения')
        mismatch_types.append(kind)
    for required in tu_lifetimes:
        candidates = [c for c in znp_lifetimes
                      if c['scope'] == required['scope'] != 'unknown'
                      and _same_parameter_context(required, c)]
        results = [_lifetime_relation(required, candidate) for candidate in candidates]
        if results and all(result == 'match' for result in results):
            continue
        mismatches.append('Срок службы/действия не подтверждён в своей области применения')
        mismatch_types.append('mismatch' if results and all(r == 'mismatch' for r in results)
                              else 'lifetime_scope_unknown')
    checked = bool(tu_power or tu_volt or tu_cat or tu_acc or tu_constraints
                   or tu_codes or tu_ranges or tu_quantities or tu_tolerances or tu_lifetimes)
    comparison_status = "match"
    if not checked or mismatches:
        comparison_status = "unknown"
    if "mismatch" in mismatch_types and not object_uncertain:
        comparison_status = "mismatch"
    numeric_required = bool(tu_power or tu_volt or tu_cat or tu_acc or tu_constraints
                            or tu_ranges or tu_quantities or tu_tolerances or tu_lifetimes)
    numeric_extracted = numeric_required
    if not checked:
        remaining = _ORDER_DATE_RE.sub(" ", _strip_clause_number(tu_text))
        remaining = _EQUIPMENT_DESIGNATION_RE.sub(' ', remaining)
        # Only an ordinal before a textual label is metadata; values in
        # other cells remain material numbers if their unit is unsupported.
        remaining = re.sub(r'(?m)^\s*\d+(?:\.\d+)*\s*\|(?=\s*[А-ЯЁA-Za-zа-яё][^|]{3,}\|)', '', remaining)
        for pattern in _IDENTIFIER_PATTERNS:
            remaining = pattern.sub(" ", remaining)
        numeric_required = bool(re.search(r"\d", remaining))
    return {
        "params_match": comparison_status == "match",
        "comparison_status": comparison_status,
        "numeric_required": numeric_required,
        "equipment_type_status": type_status,
        "numeric_applicability": ('not_applicable' if not numeric_required else
                                  ('applicable' if numeric_extracted else 'unparsed')),
        # Non-numerical obligations still use semantic/scope checks; an empty
        # numeric extractor does not certify their parameters as equal.
        "supports_safe": comparison_status == "match" or (
            not numeric_required and not mismatches
        ),
        "mismatches": mismatches,
        "mismatch_types": mismatch_types,
        "extracted": {
            "tu": {
                "power": list(tu_power),
                "voltage": list(tu_volt),
                "category": list(tu_cat),
                "accuracy": sorted(tu_acc),
                "constraints": [
                    _public_constraint(item) for item in tu_constraints
                ],
                "codes": sorted(tu_codes),
                "ranges": tu_ranges,
                "quantities": tu_quantities,
                "tolerances": tu_tolerances,
                "lifetimes": tu_lifetimes,
            },
            "znp": {
                "power": list(znp_power),
                "voltage": list(znp_volt),
                "category": list(znp_cat),
                "accuracy": sorted(znp_acc),
                "constraints": [
                    _public_constraint(item) for item in znp_constraints
                ],
                "codes": sorted(znp_codes),
                "ranges": znp_ranges,
                "quantities": znp_quantities,
                "tolerances": znp_tolerances,
                "lifetimes": znp_lifetimes,
            },
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
    *,
    znp_sources=None,
    fragments_by_id=None,
    znp_document_id: str | None = None,
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
        "znp_quote_atomic": _is_atomic_quote(
            znp_quote, verified_source_text=_verified_quote_source(
                znp_quote, znp_sources, fragments_by_id, znp_document_id,
            ),
        ),
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
