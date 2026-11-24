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
    if not Path('/current/replay.py').is_file() or not Path('/demo-export.zip').is_file():
        pytest.skip('Private current-run fixtures unavailable')
    monkeypatch.setenv('P0_DEMO_REPLAY_DIR', str(tmp_path))
    runpy.run_path('/current/replay.py', run_name='__main__')
    result = json.loads((tmp_path/'replay.json').read_text())['summary']
    entries = json.loads((tmp_path/'replay.private.json').read_text())
    assert result['new_http'] == 0 and result['source_archive_unchanged']
    assert sum(e['old_relation']=='supports' and e['new_relation']=='supports' for e in entries) == 8
    assert result['rejected'] == 5
    assert result['relations'] == {'supports': 12, 'partial': 1}
    assert result['source_coverage']['document_completeness'] == 'declared_incomplete'
    assert not result['source_coverage']['complete'] and result['fulfilled'] == 0
