"""Checked equality, contradiction and insufficient numerical evidence differ."""
import pytest

from app.requirements.tools import tool_verify_engineering_params
from app.requirements.schemas import TURequirement, ZNPClause, RequirementCategory
from app.requirements.znp_first import ZnPFirstAgent


@pytest.mark.parametrize("left,right", [
    ("Мощность станции 236 МВт.", "Мощность станции 236,0 МВт."),
    ("2 | Номинальная активная мощность | МВт | 236,0",
     "2 | Номинальная активная электрическая мощность | МВт | 236,0"),
    ("Мощность — 236 — МВт", "Мощность 236 МВт"),
    ("Диапазон коэффициента от -0,30 до +0,40 о.е.",
     "Диапазон коэффициента −0.30…+0.40 о.е."),
    ("Напряжение 110 кВ", "Напряжение 110 кВ"),
    ("Transformer power 10 MVA", "Transformer power 10 МВА"),
    ("Адрес: ул. Мира, д. 1.", "Адрес: ул. Мира, д. 1."),
])
def test_verified_match(left, right):
    result = tool_verify_engineering_params(left, right)
    assert result["comparison_status"] == "match"
    assert result["params_match"] is True


@pytest.mark.parametrize("left,right", [
    ("Расход топлива не более 240 г у.т./(кВт·ч)", "Расход топлива не более 260 г у.т./(кВт·ч)"),
    ("Время ≤ 240 с", "Время ≤ 260 с"),
    ("Время не менее 10 с", "Время не менее 5 с"),
    ("Время менее 10 с", "Время более 10 с"),
    ("Напряжение 110 кВ", "Напряжение 220 кВ"),
    ("Адрес: ул. Мира, д. 1.", "Адрес: ул. Мира, д. 2."),
])
def test_verified_mismatch(left, right):
    result = tool_verify_engineering_params(left, right)
    assert result["comparison_status"] == "mismatch"
    assert result["params_match"] is False


@pytest.mark.parametrize("left,right", [
    ("Предусмотреть защиту трансформатора", "Предусмотреть защиту трансформатора"),
    ("Мощность станции 8,6 МВт", "Мощность трансформатора 10 МВА"),
    ("Мощность станции 10 МВт", "Мощность трансформатора 10 МВт"),
    ("Длина 10 мм", "Длина 10 с"),
    ("Диапазон регулирования реактивной мощности | о.е. | −0,30…+0,40",
     "Диапазон регулирования реактивной мощности | о.е. | −0,30…+0,60"),
    ("Мощность станции от 5 до 10 МВт", "Мощность станции от 5 до 15 МВт"),
])
def test_insufficient_or_incomparable_parameters_are_unknown(left, right):
    result = tool_verify_engineering_params(left, right)
    assert result["comparison_status"] == "unknown"
    assert result["params_match"] is False


def test_missing_table_power_is_not_assumed_checked():
    result = tool_verify_engineering_params("Мощность станции | МВт | 236", "Предусмотреть станцию.")
    assert result["params_match"] is False
    assert result["comparison_status"] == "unknown"
    assert ["236", "мвт"] in [list(x) for x in result["extracted"]["tu"]["power"]]


def test_unknown_material_range_preserves_model_conflict_for_review():
    left = "Диапазон регулирования реактивной мощности | о.е. | −0,30…+0,40"
    right = "Диапазон регулирования реактивной мощности | о.е. | −0,30…+0,60"
    tu = TURequirement(id="t", clause_number="5", category=RequirementCategory.power, text=left)
    atom = ZNPClause(id="z", clause_number="5", text=right)
    link = ZnPFirstAgent._validated_link(atom, tu, dict(
        relation="conflicts", confidence=.95, tu_quote=left, znp_quote=right), .9)
    assert link["relation"] == "partial"
    assert link["proposed_relation"] == "conflicts"
    assert "engineering_params_unknown" in link["warnings"]


def test_unknown_material_range_cannot_automatically_support():
    left = "Диапазон регулирования мощности | о.е. | −0,30…+0,40"
    right = "Диапазон регулирования мощности | о.е. | −0,30…+0,60"
    tu = TURequirement(id="t", clause_number="5", category=RequirementCategory.power, text=left)
    atom = ZNPClause(id="z", clause_number="5", text=right)
    link = ZnPFirstAgent._validated_link(atom, tu, dict(
        relation="supports", confidence=.95, tu_quote=left, znp_quote=right), .9)
    assert link["relation"] == "partial"
    assert "engineering_params_unknown" in link["warnings"]


def test_object_values_cannot_be_swapped_inside_same_pair():
    result = tool_verify_engineering_params(
        "Мощность станции 10 МВт. Мощность трансформатора 20 МВт.",
        "Мощность станции 20 МВт. Мощность трансформатора 10 МВт.")
    assert result["comparison_status"] == "mismatch"
    assert result["params_match"] is False


def test_uncertain_range_does_not_erase_independent_verified_conflict():
    result = tool_verify_engineering_params(
        "Время не более 240 с. Диапазон коэффициента -0,30…+0,40 о.е.",
        "Время не более 260 с. Диапазон коэффициента -0,30…+0,60 о.е.")
    assert result["comparison_status"] == "mismatch"


def test_unparsed_material_number_cannot_be_certified_by_empty_extractor():
    result = tool_verify_engineering_params("Количество модулей 36 единиц.", "Предусмотреть модули.")
    assert result["comparison_status"] == "unknown"
    assert result["numeric_required"] is True
    assert result["supports_safe"] is False


def test_normative_code_is_not_a_range_even_with_range_in_description():
    result = tool_verify_engineering_params(
        "Диапазон регулирования по ГОСТ Р 12345-2020 для оборудования.",
        "Регулирование по ГОСТ Р 12345-2020.")
    assert result["extracted"]["tu"]["ranges"] == []
    assert result["params_match"] is True
