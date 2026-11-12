"""DeepSeek span proposals are advisory; the original source is authoritative."""
import json

from app.ai.deepseek import DeepSeekNotConfigured
from app.core.models import Fragment
from app.requirements.atomization import atomize_fragments
from app.requirements.semantic_atomization import semantic_atomize


TEXT = ("2.1. Для КЛ-1 предусмотреть алюминиевый кабель "
        "сечением не менее 120 мм² на напряжение 10 кВ.")
CONTEXT = "2.1. Для КЛ-1 предусмотреть"


def parent(text=TEXT):
    source = Fragment(
        id="f1", document_id="doc", page=3, position=0,
        block_type="paragraph", section_path=None, text=text,
    )
    return atomize_fragments("doc", [source])[0]


class Model:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def complete_json(self, *, system_prompt, user_prompt):
        self.calls += 1
        assert "clause_text" in json.loads(user_prompt)
        assert "атом" in system_prompt.lower()
        return self.payload


def proposed():
    return {"atoms": [
        {"quote": "алюминиевый кабель", "context_quotes": [CONTEXT]},
        {"quote": "сечением не менее 120 мм²", "context_quotes": [CONTEXT]},
        {"quote": "на напряжение 10 кВ.", "context_quotes": [CONTEXT]},
    ]}


def test_atomic_properties_keep_object_value_source_and_stable_identity():
    source = parent()
    result = semantic_atomize([source], Model(proposed()))
    assert result.clauses_split == 1
    assert len(result.atoms) == 3
    assert result.trace()["model_calls"] == 1
    assert all("КЛ-1" in atom.text for atom in result.atoms)
    assert all(atom.sources == source.sources for atom in result.atoms)
    assert all(atom.id != source.id for atom in result.atoms)
    assert result.atoms == semantic_atomize([source], Model(proposed())).atoms
    assert "120 мм²" in result.atoms[1].text


def test_missing_parameter_forces_whole_parent_fallback():
    missing = proposed()
    missing["atoms"].pop()
    source = parent()
    result = semantic_atomize([source], Model(missing))
    assert result.atoms == [source]
    assert result.fallback_reasons["unsplit_or_invalid_spans"] == 1


def test_model_cannot_invent_a_source_quote_or_object():
    invalid = proposed()
    invalid["atoms"][1]["quote"] = "сечением 150 мм²"
    source = parent()
    assert semantic_atomize([source], Model(invalid)).atoms == [source]


def test_model_cannot_drop_object_context_from_one_property():
    invalid = proposed()
    invalid["atoms"][1]["context_quotes"] = []
    source = parent()
    assert semantic_atomize([source], Model(invalid)).atoms == [source]


def test_model_cannot_use_ambiguous_repeated_quote():
    source = parent("2.1. КЛ-1 кабель; КЛ-1 кабель; сечение 120 мм².")
    invalid = {"atoms": [
        {"quote": "КЛ-1 кабель", "context_quotes": ["2.1. "]},
        {"quote": "сечение 120 мм².", "context_quotes": ["КЛ-1 кабель"]},
    ]}
    assert semantic_atomize([source], Model(invalid)).atoms == [source]


def test_conditional_permission_is_kept_whole_and_not_sent_to_model():
    text = "2.1. Для КЛ-1 допускается сечение 95 мм² при условии двух линий."
    source = parent(text)
    model = Model(proposed())
    result = semantic_atomize([source], model)
    assert result.atoms == [source]
    assert model.calls == 0
    assert result.fallback_reasons["conditional_or_alternative_clause"] == 1


def test_alternative_is_kept_whole():
    source = parent("2.1. КЛ-1 кабель 95 мм² или две линии 120 мм².")
    result = semantic_atomize([source], Model(proposed()))
    assert result.atoms == [source]


def test_existing_ambiguous_context_warning_prevents_split():
    source = parent()
    from dataclasses import replace
    warning = replace(source, warnings=("context_scope_requires_review",))
    model = Model(proposed())
    assert semantic_atomize([warning], model).atoms == [warning]
    assert model.calls == 0


def test_invalid_json_shape_falls_back_without_losing_source():
    source = parent()
    result = semantic_atomize([source], Model({"atoms": "bad"}))
    assert result.atoms == [source]
    assert result.clauses_split == 0


def test_provider_error_stops_further_paid_calls_but_preserves_all_clauses():
    class Offline:
        def __init__(self):
            self.calls = 0

        def complete_json(self, **kwargs):
            self.calls += 1
            raise DeepSeekNotConfigured("API key not configured")

    a = parent()
    b = atomize_fragments("doc", [
        Fragment(id="f2", document_id="doc", page=4, position=0,
                 block_type="paragraph", section_path=None,
                 text="2.2. Предусмотреть защиту оборудования."),
    ])[0]
    offline = Offline()
    result = semantic_atomize([a, b], offline)
    assert result.atoms == [a, b]
    assert result.degraded
    assert offline.calls == 1
    assert result.fallback_reasons["provider_unavailable"] == 2


def test_full_original_parent_is_retained_on_unsplittable_clause():
    source = parent("2.1. КЛ-1 напряжение 10 кВ.")
    result = semantic_atomize([source], Model({"atoms": []}))
    assert result.atoms == [source]
    assert result.trace()["atoms_total"] == 1
