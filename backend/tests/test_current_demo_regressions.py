import pytest

from app.requirements.tools import tool_verify_engineering_params as verify
from app.requirements.schemas import TURequirement, ZNPClause, RequirementCategory
from app.requirements.znp_first import ZnPFirstAgent


@pytest.mark.parametrize('a,b,status', [
    ('50 ± 0,2 Гц', '50 ± 0.2 Гц', 'match'),
    ('50 ± 0,2 Гц', '50 ± 0,5 Гц', 'unknown'),
    ('50 ± 0,2 Гц', '55 ± 0,2 Гц', 'mismatch'),
    ('50 ± 0,2 Гц', 'от 49,8 до 50,2 Гц', 'match'),
])
def test_frequency_absolute_tolerance(a, b, status):
    result = verify('Номинальная частота ' + a, 'Номинальная частота ' + b)
    assert result['comparison_status'] == status


@pytest.mark.parametrize('right,status', [('40 лет', 'match'), ('30 лет', 'mismatch'), ('50 лет', 'match')])
def test_service_life_lower_bound(right, status):
    result = verify('Срок службы оборудования не менее 40 лет', 'Срок службы оборудования ' + right)
    assert result['comparison_status'] == status


def test_tabular_tolerance_and_service_years():
    for text in ['8 | Номинальная частота | Гц | 50 ± 0,2',
                 '14 | Срок службы оборудования | лет | 40']:
        assert verify(text, text)['comparison_status'] == 'match'
    assert verify('Срок службы оборудования 40 лет', 'Срок действия документа 40 лет')['comparison_status'] == 'unknown'


@pytest.mark.parametrize('name', ['ПГУ-236', 'ТДН-250000/110', 'ТЗФГ-236-2У3'])
def test_equipment_designations_are_not_material_quantities(name):
    result = verify('1 | Тип оборудования | — | ' + name, 'Тип оборудования ' + name)
    assert result['comparison_status'] == 'unknown'
    assert result['numeric_applicability'] == 'not_applicable'
    assert result['supports_safe']
    assert verify(name + ' номинальной мощностью 236 МВт', name + ' номинальной мощностью 236 МВт')['params_match']


@pytest.mark.parametrize('text,quote', [
    ('Тип установки | ПГУ-236', 'Номинальная мощность 236 МВт'),
    ('Тип установки | ПГУ-236', 'В составе РЗА энергоблока ПГУ-236 должны быть реализованы функции, приведённые в таблице 12.1.'),
    ('Предусмотреть МТЗ блока 110 кВ.', 'В составе РЗА должны быть реализованы функции, приведённые в таблице 12.1.'),
    ('Предусмотреть АПВ линий 110 кВ.', 'В составе РЗА должны быть реализованы функции, приведённые в таблице 12.1.'),
])
def test_object_or_generic_function_reference_is_not_partial_evidence(text, quote):
    tu = TURequirement(id='t', clause_number='1', category=RequirementCategory.other, text=text)
    atom = ZNPClause(id='z', clause_number='1', text=quote)
    assert ZnPFirstAgent._validated_link(atom, tu, dict(relation='partial', confidence=.9,
        tu_quote=text, znp_quote=quote), .8) is None


def test_true_partial_function_evidence_remains():
    tu = TURequirement(id='t', clause_number='1', category=RequirementCategory.protection,
                       text='Предусмотреть МТЗ и АПВ блока 110 кВ.')
    atom = ZNPClause(id='z', clause_number='2', text='Предусмотреть максимальную токовую защиту блока 110 кВ.')
    link = ZnPFirstAgent._validated_link(atom, tu, dict(relation='partial', confidence=.9,
        tu_quote=tu.text, znp_quote=atom.text), .8)
    assert link['relation'] == 'partial'


def test_caption_is_context_and_declared_incomplete_still_blocks_fulfilled():
    from app.core.models import Fragment
    from app.requirements.atomization import atomize_fragments
    from app.requirements.source_coverage import source_coverage_ledger
    fragments = [Fragment(id='r', document_id='d', page=1, position=0,
                          block_type='table_row', container_id='table-1',
                          text='12 | АПВ линий 110 кВ | Приказ № 546'),
                 Fragment(id='c', document_id='d', page=1, position=1,
                          block_type='paragraph', container_id=None,
                          text='Таблица 4 — Функции РЗА (показаны только выбранные исходные строки)')]
    atoms = atomize_fragments('d', fragments)
    assert len(atoms) == 1 and 'Таблица' not in atoms[0].text
    assert {s.fragment_id for s in atoms[0].context_sources} == {'c'}
    ledger = source_coverage_ledger(fragments, atoms)
    assert ledger['fragment_coverage_complete'] and not ledger['complete']
    assert ledger['document_completeness'] == 'declared_incomplete'


def test_model_phase_times_are_preserved_on_failure():
    from app.requirements.llm_tracking import TrackedRequirementsLLM
    class Broken:
        def complete_json(self, **kwargs):
            raise ValueError('sentinel')
    ledger = TrackedRequirementsLLM(Broken(), max_calls=2)
    with pytest.raises(ValueError):
        ledger.complete_json(system_prompt='test', user_prompt='{"znp_atom":{}}')
    assert ledger.trace()['elapsed_ms_by_phase']['matching'] >= 0
    assert ledger.trace()['errors_by_phase']['matching'] == 1


def test_retrieval_and_validation_stage_times_are_saved():
    from app.ai.embeddings import EmbeddingService
    class Empty:
        def complete_json(self, **kwargs):
            return dict(links=[], conflicts=[], uncertain_ids=[], covered=False)
    req = TURequirement(id='t', clause_number='1', category=RequirementCategory.power, text='Мощность 236 МВт.')
    atom = ZNPClause(id='z', clause_number='1', text=req.text)
    result = ZnPFirstAgent(ai_client=Empty(), embedder=EmbeddingService(enabled=False),
                           targeted_enabled=False).verify_all([req], [atom])
    timings = result.trace_info['local_timings_ms']
    assert timings['document_embeddings'] >= 0
    assert timings['ranking'] >= 0
    assert timings['link_validation'] >= 0


def test_table_related_obligation_and_cross_table_scope_are_not_filtered():
    from app.core.models import Fragment
    from app.requirements.atomization import atomize_fragments
    fs = [Fragment(id='a', document_id='d', page=1, position=0, block_type='table_row',
                   container_id='A', text='1 | Мощность | 236 МВт'),
          Fragment(id='c', document_id='d', page=1, position=1, block_type='paragraph',
                   text='Таблица 1 — Мощность установки'),
          Fragment(id='b', document_id='d', page=1, position=2, block_type='table_row',
                   container_id='B', text='1 | Напряжение | 110 кВ'),
          Fragment(id='p', document_id='d', page=1, position=3, block_type='paragraph',
                   text='Таблица 2 должна содержать ограничения напряжения.')]
    atoms = atomize_fragments('d', fs)
    second = next(a for a in atoms if any(s.fragment_id == 'b' for s in a.sources))
    assert 'c' not in {s.fragment_id for s in second.context_sources}
    assert any(s.fragment_id == 'p' for a in atoms for s in a.sources)


@pytest.mark.parametrize('text', ['Пункт 6.3.2. Защита трансформатора.',
                                  'Приказ Минэнерго от 10.07.2020 № 546.',
                                  'ГОСТ Р 58983-2020.'])
def test_non_physical_number_metadata_is_not_failed_physical_extraction(text):
    result = verify(text, text)
    assert result['numeric_applicability'] == 'not_applicable'


def test_current_saved_demo_replay_is_source_grounded(tmp_path, monkeypatch):
    import json
    import runpy
    from pathlib import Path
    import os
    runner = os.environ.get('P0_DEMO_REPLAY_SCRIPT')
    archive = os.environ.get('P0_DEMO_REPLAY_ARCHIVE')
    if not runner or not archive or not Path(runner).is_file() or not Path(archive).is_file():
        pytest.skip('Optional private replay: set P0_DEMO_REPLAY_SCRIPT and P0_DEMO_REPLAY_ARCHIVE')
    monkeypatch.setenv('P0_DEMO_REPLAY_DIR', str(tmp_path))
    runpy.run_path(runner, run_name='__main__')
    result = json.loads((tmp_path/'replay.json').read_text())['summary']
    entries = json.loads((tmp_path/'replay.private.json').read_text())
    assert result['new_http'] == 0 and result['source_archive_unchanged']
    assert sum(e['old_relation']=='supports' and e['new_relation']=='supports' for e in entries) == 8
    assert result['rejected'] == 5
    assert result['relations'] == {'supports': 12, 'partial': 1}
    assert result['source_coverage']['document_completeness'] == 'declared_incomplete'
    assert not result['source_coverage']['complete'] and result['fulfilled'] == 0


@pytest.mark.parametrize("left,right,status", [
    ("Частота генератора 50 ± 0,2 Гц", "Частота внешней сети 50 ± 0,2 Гц", "unknown"),
    ("Частота генератора 50 ± 0,2 Гц", "Частота внешней сети 55 ± 0,2 Гц", "unknown"),
    ("Мощность генератора 236 ± 1 МВт", "Мощность генератора от 235 до 237 МВт", "match"),
    ("Напряжение генератора 10 ± 0,5 кВ", "Напряжение генератора от 9,5 до 10,5 кВ", "match"),
    ("Напряжение генератора от 9,5 до 10,5 кВ", "Напряжение генератора 10 ± 0,5 кВ", "match"),
    ("Срок службы трансформатора 40 лет", "Срок службы генератора 40 лет", "unknown"),
    ("Срок службы трансформатора не менее 40 лет", "Срок службы трансформатора не менее 30 лет", "unknown"),
    ("Срок службы трансформатора не менее 40 лет", "Срок службы трансформатора не менее 50 лет", "match"),
    ("Срок службы трансформатора не менее 40 лет", "Срок службы трансформатора не более 30 лет", "mismatch"),
    ("Срок службы трансформатора более 40 лет", "Срок службы трансформатора 40 лет", "mismatch"),
    ("Срок службы трансформатора 40 лет", "Срок службы трансформатора 30 лет", "mismatch"),
    ("Срок службы трансформатора не менее 40 лет", "Срок службы трансформатора 50 лет", "match"),
])
def test_scoped_numeric_review_cases(left, right, status):
    assert verify(left, right)["comparison_status"] == status


def test_different_explicit_type_is_conflict_not_absent_evidence():
    from app.requirements.coverage import conflict_candidates
    tu = TURequirement(id="t", clause_number="1", category=RequirementCategory.other,
                       text="Тип установки | ПГУ-236")
    atom = ZNPClause(id="z", clause_number="1", text="Тип установки | ПГУ-400")
    assert verify(tu.text, atom.text)["comparison_status"] == "mismatch"
    link = ZnPFirstAgent._validated_link(atom, tu, dict(
        relation="conflicts", confidence=.95, tu_quote=tu.text, znp_quote=atom.text), .9)
    assert link is not None and link["relation"] == "conflicts"
    assert [c["atom_id"] for c in conflict_candidates(tu, [atom])] == ["z"]
    support = ZnPFirstAgent._validated_link(atom, tu, dict(
        relation="supports", confidence=.95, tu_quote=tu.text, znp_quote=atom.text), .9)
    assert support is None or support["relation"] != "supports"


def test_caption_between_two_table_containers_is_not_dual_context():
    from app.core.models import Fragment
    from app.requirements.atomization import atomize_fragments
    from app.requirements.source_coverage import source_coverage_ledger
    def fragment(id_, pos, text, block, container=None):
        return Fragment(id=id_, document_id="d", page=1, position=pos,
                        text=text, block_type=block, container_id=container)
    fragments = [
        fragment("a", 0, "1 | Мощность | 236 МВт", "table_row", "A"),
        fragment("c", 1, "Таблица 2 — Параметры напряжения", "paragraph"),
        fragment("h", 2, "№ | Параметр | Значение", "table_row", "B"),
        fragment("b", 3, "1 | Напряжение | 110 кВ", "table_row", "B"),
    ]
    atoms = atomize_fragments("d", fragments)
    assert len(atoms) == 2
    assert all("c" not in {s.fragment_id for s in atom.context_sources} for atom in atoms)
    ledger = source_coverage_ledger(fragments, atoms)
    assert "c" in ledger["unclassified_fragment_ids"] and not ledger["complete"]


@pytest.mark.parametrize("candidate", [
    "Тип установки ПГУ-236 или ПГУ-400",
    "Тип установки ПГУ-400 не применяется",
    "Блок 2: Тип установки ПГУ-400",
])
def test_type_field_does_not_certify_alternative_or_qualified_conflict(candidate):
    from app.requirements.tools import equipment_type_comparison
    assert equipment_type_comparison("Тип установки ПГУ-236", candidate) == "unknown"


def test_named_function_with_table_reference_remains_evidence():
    tu = TURequirement(id="t", clause_number="1", category=RequirementCategory.protection,
                       text="Реализовать МТЗ блока.")
    atom = ZNPClause(id="z", clause_number="2",
                     text="Должны быть реализованы МТЗ и АПВ, приведённые в таблице 12.1.")
    link = ZnPFirstAgent._validated_link(atom, tu, dict(
        relation="partial", confidence=.9, tu_quote=tu.text, znp_quote=atom.text), .9)
    assert link is not None and link["relation"] == "partial"


def test_disagreeing_intervals_of_same_parameter_require_review():
    result = verify("Частота генератора 50 ± 0,2 Гц",
                    "Частота генератора 50 ± 0,2 Гц.\nЧастота генератора 55 ± 0,2 Гц.")
    assert result["comparison_status"] == "unknown"


@pytest.mark.parametrize("left,right,status", [
    ("Диапазон регулирования мощности | % | 30…100",
     "Диапазон регулирования мощности от 30 до 100 %", "match"),
    ("Диапазон регулирования мощности | % | 30…100",
     "Диапазон регулирования мощности | % | 30…85", "unknown"),
    ("Диапазон коэффициента | о.е. | −0,30…+0,40",
     "Диапазон коэффициента от -0,30 до +0,40 о.е.", "match"),
    ("Частота генератора Г-1 50 ± 0,2 Гц",
     "Частота генератора Г-2 50 ± 0,2 Гц", "unknown"),
    ("Срок службы генератора 1 40 лет",
     "Срок службы генератора 2 40 лет", "unknown"),
])
def test_table_intervals_and_explicit_object_identifiers(left, right, status):
    result = verify(left, right)
    assert result["comparison_status"] == status


def test_table_reference_can_itself_be_an_obligation():
    text = "Должны быть реализованы функции, приведённые в таблице 12.1."
    tu = TURequirement(id="t", clause_number="1", category=RequirementCategory.protection,
                       text=text)
    atom = ZNPClause(id="z", clause_number="2", text=text)
    link = ZnPFirstAgent._validated_link(atom, tu, dict(
        relation="partial", confidence=.9, tu_quote=text, znp_quote=text), .9)
    assert link is not None and link["relation"] == "partial"
