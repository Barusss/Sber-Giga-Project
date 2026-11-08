from app.requirements.tools import tool_verify_engineering_params


def test_power_shared_unit_is_extracted_for_both_values():
    result = tool_verify_engineering_params(
        "Мощность 365 МВт и 601 МВт.",
        "Мощность с 365 до 601 МВт.",
    )
    assert result["params_match"]


def test_power_shared_unit_detects_different_first_value():
    result = tool_verify_engineering_params(
        "Мощность 365 МВт и 601 МВт.",
        "Мощность с 350 до 601 МВт.",
    )
    assert not result["params_match"]


def test_power_shared_unit_does_not_interpret_energy_as_power():
    result = tool_verify_engineering_params(
        "Выработка от 365 до 601 МВт·ч.",
        "Выработка от 365 до 601 МВт·ч.",
    )
    assert result["extracted"]["tu"]["power"] == []


def test_energy_with_spaces_does_not_become_power():
    result = tool_verify_engineering_params(
        "Выработка от 365 до 601 МВт · ч.",
        "Выработка от 365 до 601 МВт · ч.",
    )
    assert result["extracted"]["tu"]["power"] == []
