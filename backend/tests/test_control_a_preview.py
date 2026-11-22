"""Optional original-DOCX control A replay; private fixtures stay outside Git."""
import json
from collections import Counter
from pathlib import Path
import pytest
from sqlalchemy import select
from app.core.db import get_db
from app.core.models import Fragment
from app.main import app

@pytest.mark.skipif(not Path('/old/private-cases.json').is_file(), reason='Private PR51 original fixtures unavailable')
def test_original_control_a_diagnostic_both_apis(client, monkeypatch):
    old=Path('/old')
    case=json.loads((old/'private-cases.json').read_text())['A']
    trace=next((old/'live-traces').iterdir())/'private'
    saved=json.loads((trace/'experiment-A-legacy-matching-response.json').read_text())['reply']
    observed=[]; active=['']
    def fake(self, *, system_prompt, user_prompt):
        p=json.loads(user_prompt)
        atom=p.get('znp_atom',{})
        if atom.get('text')==case['z']['text']:
            candidates=p.get('tu_candidates',[])
            found=[(i,c) for i,c in enumerate(candidates,1) if c['text']==case['t']['text']]
            if found:
                rank,c=found[0]
                assert [s['text'] for s in atom['source_fragments']]==[s['text'] for s in case['z']['sources']]
                assert [s['text'] for s in c['source_fragments']]==[s['text'] for s in case['t']['sources']]
                assert [s['text'] for s in atom['context_sources']]==[s['text'] for s in case['z']['context_sources']]
                # Explicit adapted test replay after exact text/source proof.
                # No production ID, source, candidate or rank is changed.
                reply=json.loads(json.dumps(saved))
                assert reply['links'][0]['requirement_id']==case['t']['id']
                reply['links'][0]['requirement_id']=c['requirement_id']
                observed.append(dict(api=active[0],phase=p['phase'],rank=rank,candidates=len(candidates),adapted_test_replay=True))
                return reply
        return {'links':[], 'atoms':[], 'search_more':False, 'covered':False,
                'object_consistent':False,'evidence_ids':[], 'conflicts':[], 'uncertain_ids':[]}
    monkeypatch.setattr('app.ai.deepseek.DeepSeekClient.complete_json',fake)
    pid=client.post('/api/v1/projects',json={'name':'Private control A preview'}).json()['id']
    docs={}
    for kind in ('tu','znp'):
        r=client.post(f'/api/v1/projects/{pid}/documents',data={'kind':kind},files={'file':(kind+'.docx',Path('/inputs/'+kind+'.docx').read_bytes(),'application/vnd.openxmlformats-officedocument.wordprocessingml.document')})
        assert r.status_code==201
        docs[kind]=r.json()['id']
    with next(app.dependency_overrides[get_db]()) as db:
        sources=db.scalars(select(Fragment).where(Fragment.document_id==docs['znp'])).all()
        matches=[f for f in sources if f.text.strip()==case['z']['sources'][0]['text']]
        assert len(matches)==1
        source_id=matches[0].id
    root=f'/api/v1/projects/{pid}/comparisons'
    pair=client.post(root,json={'pairs':[{'tu_document_id':docs['tu'],'znp_document_id':docs['znp']}]}).json()[0]['id']
    results={}; summaries={}
    for api in ('runs','analyze'):
        active[0]=api
        r=client.post(f'{root}/{pair}/runs',params={'test_mode':True,'preview_znp_source_id':source_id}) if api=='runs' else client.post('/api/v1/requirements/analyze',json={'project_id':pid,'test_mode':True,'preview_znp_source_id':source_id})
        assert r.status_code==(201 if api=='runs' else 200),r.text
        data=r.json(); result=data['result'] if api=='runs' else data
        if api=='runs':
            assert not data['run']['is_current']
            assert client.get(f'{root}/{pair}/runs/'+data['run']['id']).json()['result']==result
        trace=result['trace_info']
        assert trace['test_mode']['isolated'] and trace['test_mode']['diagnostic_only']
        assert trace['source_coverage']['complete'] is False
        assert result['stats']['fulfilled']==0
        control=[a for a in trace['znp_atom_results'] if a['text']==case['z']['text']]
        assert len(control)==1
        with next(app.dependency_overrides[get_db]()) as db:
            for source in control[0]['sources'] + control[0]['context_sources']:
                fragment = db.get(Fragment, source['fragment_id'])
                assert fragment.document_id == docs['znp']
                assert fragment.text[source['start_offset']:source['end_offset']] == source['text']
        links=[l for l in control[0]['links'] if l['relation']=='supports']
        assert len(links)==1
        assert links[0]['tu_quote']==saved['links'][0]['tu_quote']
        assert links[0]['znp_quote']==saved['links'][0]['znp_quote']
        results[api]=result
        summaries[api]=dict(http=r.status_code,stats=result['stats'],supports=len(links),source_coverage_complete=False,unclassified=trace['source_coverage']['unclassified_count'])
    assert len(observed)==2 and all(x['rank']==1 for x in observed)
    assert client.get(f'{root}/{pair}').json()['current_run_id'] is None
    for endpoint in ('tu-requirements','verifications'):
        assert client.get(f'/api/v1/requirements/projects/{pid}/{endpoint}').json()==[]
    assert client.get(f'/api/v1/projects/{pid}/findings').json()==[]
    assert client.post(f'{root}/{pair}/runs',params={'preview_znp_source_id':source_id}).status_code==422
    assert client.post('/api/v1/requirements/analyze',json={'project_id':pid,'preview_znp_source_id':source_id}).status_code==422
    assert client.post(f'{root}/{pair}/runs',params={'test_mode':True,'preview_znp_source_id':'foreign-source'}).status_code==422
    if Path('/out').is_dir():
        Path('/out/control-a-preview.private.json').write_text(json.dumps(results,ensure_ascii=False))
        Path('/out/control-a-preview.json').write_text(json.dumps({'apis':summaries,'replay':observed,'deepseek_http':0},ensure_ascii=False,indent=2))
    print(json.dumps({'apis':summaries,'replay':observed,'deepseek_http':0},ensure_ascii=False))


def test_project_analyze_uses_znp_first_but_explicit_lists_keep_legacy(client, monkeypatch):
    from app.requirements.znp_first import ZnPFirstAgent
    from app.requirements.agent import RequirementsAgent
    from app.requirements.schemas import CheckRequirementsResponse
    response = CheckRequirementsResponse(checked_requirements=[], stats={'total': 0})
    seen = []
    def analyze(payload, db, agent):
        seen.append(type(agent))
        return response
    monkeypatch.setattr('app.requirements.router._lock_project_for_analysis', lambda *args: None)
    monkeypatch.setattr('app.requirements.router._analyze_project_requirements', analyze)
    assert client.post('/api/v1/requirements/analyze', json={'project_id':'test-project'}).status_code == 200
    assert seen == [ZnPFirstAgent]
    def legacy(self, tu, znp):
        seen.append(type(self))
        return response
    monkeypatch.setattr(RequirementsAgent, 'verify_all', legacy)
    assert client.post('/api/v1/requirements/analyze', json={'tu_requirements':[], 'znp_clauses':[]}).status_code == 200
    assert seen[-1] == RequirementsAgent
