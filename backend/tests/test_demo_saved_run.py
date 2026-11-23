"""Optional original demo fixtures; replay never contacts a model provider."""
import json
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

import pytest

from app.requirements.extraction import is_table_metadata
from app.requirements.schemas import TURequirement, ZNPClause
from app.requirements.znp_first import ZnPFirstAgent


@pytest.mark.skipif(not Path('/demo-export.zip').is_file(), reason='Private demo export unavailable')
def test_saved_demo_header_links_rejected_real_functions_and_numbers_preserved():
    with ZipFile('/demo-export.zip') as z:
        result = json.loads(z.read('result.json'))
    atoms = {a['atom_id']: ZNPClause.model_validate(dict(a, id=a['atom_id']))
             for a in result['trace_info']['znp_atom_results']}
    header_rejections = 0
    functions = set()
    numeric_clauses = set()
    for item in result['checked_requirements']:
        req = TURequirement.model_validate(item['requirement'])
        for saved in item['validation_details']['matches']:
            counts = Counter()
            link = ZnPFirstAgent._validated_link(atoms[saved['atom_id']], req, dict(
                relation=saved['relation'], confidence=saved['confidence'],
                tu_quote=saved['tu_quote'], znp_quote=saved['quote']), 0, counts)
            if req.text == '№ | Функция РЗА / ПА | Нормативный документ':
                assert link is None and counts['structural_metadata_not_obligation'] == 1
                header_rejections += 1
            if link and link['relation'] == 'supports':
                for name in ['МТЗ', 'ЗМН', 'УРОВ', 'АПВ']:
                    if name in req.text:
                        functions.add(name)
                if req.clause_number in {'2', '6', '7'}:
                    check = link['param_check']
                    assert check['comparison_status'] == 'match'
                    assert check['params_match']
                    assert check['extracted']['tu']['power'] or check['extracted']['tu']['voltage']
                    numeric_clauses.add(req.clause_number)
    assert header_rejections == 5
    assert functions == {'МТЗ', 'ЗМН', 'УРОВ', 'АПВ'}
    assert numeric_clauses == {'2', '6', '7'}


@pytest.mark.skipif(not Path('/demos').is_dir(), reason='Original demo DOCX unavailable')
def test_demo_both_project_apis_are_diagnostic_and_keep_unknown_sources(client, monkeypatch):
    calls = []
    def offline(self, **kwargs):
        calls.append(1)
        return dict(atoms=[], links=[], covered=False, object_consistent=False,
                    evidence_ids=[], conflicts=[], uncertain_ids=[], search_more=False)
    monkeypatch.setattr('app.ai.deepseek.DeepSeekClient.complete_json', offline)
    pid = client.post('/api/v1/projects', json={'name': 'Isolated original demo'}).json()['id']
    docs = {}
    for kind in ['tu', 'znp']:
        file = next(Path('/demos').glob('01_*_' + kind.upper() + '.docx'))
        uploaded = client.post(f'/api/v1/projects/{pid}/documents', data={'kind': kind},
                               files={'file': (file.name, file.read_bytes(), 'application/octet-stream')})
        assert uploaded.status_code == 201
        docs[kind] = uploaded.json()['id']
    root = f'/api/v1/projects/{pid}/comparisons'
    pair = client.post(root, json={'pairs': [{'tu_document_id': docs['tu'],
                                            'znp_document_id': docs['znp']}]}).json()[0]['id']
    first = client.post(f'{root}/{pair}/runs', params={'test_mode': True})
    second = client.post('/api/v1/requirements/analyze', json={'project_id': pid, 'test_mode': True})
    assert first.status_code == 201 and second.status_code == 200
    for result in [first.json()['result'], second.json()]:
        assert result['stats']['fulfilled'] == 0
        assert all(not is_table_metadata(i['requirement']['text']) for i in result['checked_requirements'])
        assert result['trace_info']['test_mode']['diagnostic_only']
        assert not result['trace_info']['source_coverage']['complete']
    assert client.get(f'{root}/{pair}').json()['current_run_id'] is None
    assert client.get(f'/api/v1/requirements/projects/{pid}/verifications').json() == []
    # The real full-run planner must reject an insufficient request budget
    # before even the local fake model is invoked, through BOTH routes.
    from app.core.config import settings
    monkeypatch.setattr(settings, 'requirements_llm_max_calls', 1)
    before = len(calls)
    refused = [client.post(f'{root}/{pair}/runs', params={'allow_incomplete_sources': True}),
               client.post('/api/v1/requirements/analyze', json={
                   'project_id': pid, 'allow_incomplete_sources': True})]
    assert all(r.status_code == 422 and 'global_cap_below_first_pass_baseline' in r.text
               for r in refused)
    assert len(calls) == before
