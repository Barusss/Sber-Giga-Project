"""Инструменты ИИ-агента для сверки требований ТУ и пунктов ЗнП."""
import math
import re
from typing import Any

from app.requirements.schemas import RequirementCategory, ZNPClause


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
def tool_search_znp_clauses(
    query: str,
    znp_clauses: list[ZNPClause],
    top_k: int = 3,
    query_embedding: list[float] | None = None,
    clause_embeddings: list[list[float]] | None = None,
    semantic_weight: float = 0.6,
) -> list[dict[str, Any]]:
    """Ищет релевантные пункты ЗнП; без embeddings работает как раньше."""
    norm_query = query.lower().replace("ё", "е")
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
        norm_clause = clause.text.lower().replace("ё", "е")
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
    if tu_volt and znp_volt and tu_volt != znp_volt:
        mismatches.append(f"Несовпадение напряжения: ТУ={tu_volt}, ЗнП={znp_volt}")

    # 3. Категория надёжности
    cat_re = r"\b(I{1,3}|1|2|3)\s*(?:-?[аяе]?\s*)?категори"
    cat_map = {"1": "I", "2": "II", "3": "III", "i": "I", "ii": "II", "iii": "III"}
    tu_cat = {cat_map.get(c.lower(), c.upper()) for c in re.findall(cat_re, tu_text, re.IGNORECASE)}
    znp_cat = {cat_map.get(c.lower(), c.upper()) for c in re.findall(cat_re, znp_text, re.IGNORECASE)}
    if tu_cat and znp_cat and tu_cat != znp_cat:
        mismatches.append(f"Несовпадение категории надёжности: ТУ={tu_cat}, ЗнП={znp_cat}")

    # 4. Класс точности учёта
    acc_re = r"класс[ауеом]*\s+точност[иью]\s+(?:не\s+ниже\s+)?(\d+(?:[.,]\d+)?[sSсС]?)"
    tu_acc = {m.replace(",", ".").upper().replace("С", "S") for m in re.findall(acc_re, tu_text, re.IGNORECASE)}
    znp_acc = {m.replace(",", ".").upper().replace("С", "S") for m in re.findall(acc_re, znp_text, re.IGNORECASE)}
    if tu_acc and znp_acc and tu_acc != znp_acc:
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
# ИНСТРУМЕНТ 4: Валидатор точности доказательных цитат
# ---------------------------------------------------------------------------
def tool_validate_quotes(
    tu_full_text: str,
    tu_quote: str | None,
    znp_full_text: str | None,
    znp_quote: str | None,
) -> dict[str, Any]:
    """Проверяет, что цитаты реально присутствуют в исходных фрагментах документов."""
    def normalize(s: str) -> str:
        return " ".join(s.replace("ё", "е").split()).casefold()

    tu_valid = bool(tu_quote and normalize(tu_quote) in normalize(tu_full_text))
    znp_valid = True
    if znp_quote:
        znp_valid = bool(znp_full_text and normalize(znp_quote) in normalize(znp_full_text))

    return {
        "is_valid": tu_valid and znp_valid,
        "tu_quote_valid": tu_valid,
        "znp_quote_valid": znp_valid,
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