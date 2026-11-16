"""Deterministic selection of a SMALL technical preview sample.

This is NOT used by the full comparison. Excluding a clause here must never
be interpreted as proof that the clause is irrelevant to the real project.
"""
from __future__ import annotations

import re
from typing import TypeVar, Sequence

from app.requirements.tools import _list_items

T = TypeVar("T")

_TECH = re.compile(
    r"(?:квт|ква|кв\b|мм(?:²|2)|кабел\w*|линии?\b|электр\w*|"
    r"присоедин\w*|мощност\w*|напряжени\w*|сечени\w*|"
    r"счетчик\w*|уч[её]т\w*|трансформатор\w*|рза\b|"
    r"защит\w*|оборудовани\w*|категори\w*|резерв\w*|"
    r"провод\w*|автомат\w*|сети?\b|выключател\w*|"
    r"подстанц\w*|нагрузк\w*|заземлени\w*)",
    re.IGNORECASE,
)
_ACTION = re.compile(
    r"(?:предусмотр\w*|установ\w*|обеспеч\w*|подключ\w*|"
    r"выполн\w*|провест\w*|размест\w*|примен\w*|"
    r"проектир\w*|оснаст\w*|использ\w*|допуска\w*|"
    r"должн\w*|необходим\w*|требу\w*)",
    re.IGNORECASE,
)
_REFERENCE = re.compile(r"\b(?:ГОСТ|СНиП|СП\s*\d|ФЗ\b|ПУЭ|приказ|постановлени)\b", re.IGNORECASE)
_LEGAL_HEADER = re.compile(
    r"\b(?:нормативн\w*|правов\w*|документ\w*|законодательств\w*|"
    r"перечень|ссылк\w*|основани\w*|преамбул\w*)\b",
    re.IGNORECASE,
)
_VALUE = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:квт|ква|кв|мм[²2]?|а\b|м\b)\b", re.IGNORECASE)


def _eligible(text: str) -> bool:
    if not _TECH.search(text):
        return False
    if len(text) > 1300:
        return False
    # Normative-act catalogs or legal preambles: these can explode one TU
    # clause into 100+ unrelated obligations and burn the coverage budget.
    if _LEGAL_HEADER.search(text) and len(_REFERENCE.findall(text)) >= 2:
        return False
    if len(_REFERENCE.findall(text)) >= 4:
        return False
    if len(_list_items(text)) > 2:
        return False
    return bool(_ACTION.search(text) or _VALUE.search(text))


def select_technical_preview(clauses: Sequence[T], *, limit: int = 10) -> list[T]:
    """First N EXTRACTED TECHNICAL clauses in source order, not first N pages."""
    return [clause for clause in clauses if _eligible(clause.text)][:limit]
