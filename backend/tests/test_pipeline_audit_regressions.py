"""Correctness expectations that fail on dev@18488f4 (audit, not fixes)."""
from io import BytesIO
from types import SimpleNamespace
import json

import fitz
from docx import Document as WordDocument
from PIL import Image
import pytest

from app.documents.chunking import build_chunks
from app.documents.parser import extract_blocks
from app.requirements.agent import RequirementsAgent
from app.requirements.extraction import extract_clauses
from app.requirements.schemas import RequirementCategory, TURequirement, ZNPClause
from app.normatives.extraction import fact_value, find_candidates


def make_docx(lines):
    doc = WordDocument()
    for line in lines:
        doc.add_paragraph(line)
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def upload(client, project, kind, lines):
    response = client.post(
        f'/api/v1/projects/{project}/documents', data={'kind': kind},
        files={'file': (kind + '.docx', make_docx(lines), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
    )
    assert response.status_code == 201, response.text
    return response.json()['id']


def analyze(client, project, tu, znp):
    response = client.post('/api/v1/requirements/analyze', json={
        'project_id': project, 'tu_document_id': tu, 'znp_document_id': znp,
    })
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(('tu', 'znp'), [
    ('Предусмотреть резервное питание системы 24 В.', 'Не предусматривать резервное питание системы 24 В.'),
    ('Для трансформатора А мощность 10 МВт, для трансформатора Б мощность 20 МВт.',
     'Для трансформатора А мощность 20 МВт, для трансформатора Б мощность 10 МВт.'),
    ('Установить два независимых канала связи.', 'Установить один независимый канал связи.'),
    ('Установить защиту трансформатора.', 'Не устанавливать защиту трансформатора.'),
])
def test_contradiction_never_fulfilled_offline(tu, znp):
    result = RequirementsAgent().verify_all(
        [TURequirement(id='tu', clause_number='1.1', category=RequirementCategory.other, text=tu)],
        [ZNPClause(id='znp', clause_number='1.1', text=znp)],
    )
    assert result.checked_requirements[0].status.value != 'fulfilled'


def test_mixed_case_body_after_toc_is_indexed():
    blocks = extract_blocks(make_docx([
        'СОДЕРЖАНИЕ', '1. Общие сведения ..... 2',
        '1. Общие сведения', '1.1. Предусмотреть питание 24 В.',
    ]), '.docx')
    assert any('Предусмотреть питание 24 В.' in chunk.text for chunk in build_chunks(blocks))


def test_identical_requirements_under_different_equipment_are_kept():
    blocks = extract_blocks(make_docx([
        '1. ПИТАНИЕ НАСОСНОЙ СТАНЦИИ', '1.1. Предусмотреть питание 24 В.',
        '2. ПИТАНИЕ СИСТЕМЫ УПРАВЛЕНИЯ', '2.1. Предусмотреть питание 24 В.',
    ]), '.docx')
    for i, block in enumerate(blocks):
        object.__setattr__(block, 'id', str(i))
    assert len(extract_clauses(blocks)) == 2


def test_blank_document_is_not_successfully_indexed(client):
    project = client.post('/api/v1/projects', json={'name': 'Blank'}).json()['id']
    response = client.post(
        f'/api/v1/projects/{project}/documents', data={'kind': 'tu'},
        files={'file': ('blank.docx', make_docx([]), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
    )
    assert response.status_code == 422


def test_other_tu_results_survive_another_analysis(client):
    project = client.post('/api/v1/projects', json={'name': 'Multiple TU'}).json()['id']
    tu1 = upload(client, project, 'tu', ['1.1. Установить питание 24 В.'])
    tu2 = upload(client, project, 'tu', ['2.1. Установить наружное освещение.'])
    znp = upload(client, project, 'znp', ['1.1. Установить питание 24 В.', '2.1. Установить наружное освещение.'])
    analyze(client, project, tu1, znp)
    analyze(client, project, tu2, znp)
    rows = client.get(f'/api/v1/requirements/projects/{project}/tu-requirements').json()
    assert tu1 in {row['document_id'] for row in rows}


def test_human_verdict_for_one_znp_does_not_confirm_another(client):
    project = client.post('/api/v1/projects', json={'name': 'Changed ZnP'}).json()['id']
    tu = upload(client, project, 'tu', ['1.1. Предусмотреть питание 24 В.'])
    znp1 = upload(client, project, 'znp', ['1.1. Предусмотреть питание 24 В.'])
    znp2 = upload(client, project, 'znp', ['1.1. Установить наружное освещение.'])
    original = analyze(client, project, tu, znp1)['checked_requirements'][0]
    confirmed = client.post(f'/api/v1/requirements/projects/{project}/confirm', json={
        'requirement_id': original['requirement']['id'], 'status': 'fulfilled',
    })
    assert confirmed.status_code == 200
    current = analyze(client, project, tu, znp2)['checked_requirements'][0]
    assert not (current['status'] == 'fulfilled' and current['is_confirmed_by_human'])


def test_mandatory_gost_in_tu_is_not_an_informative_reference():
    fragment = SimpleNamespace(id='f', page=1, position=0, text='1.1. Предусмотреть напряжение согласно ГОСТ 29322-2014.')
    chunks = [SimpleNamespace(sources=[SimpleNamespace(fragment=fragment)])]
    candidates, _ = find_candidates(chunks)
    assert fact_value(candidates[0]['code'], candidates)['reference_kind'] == 'normative'


def test_equivalent_registry_designations_cannot_duplicate(client):
    payload = {'code': 'ГОСТ 29322-2014', 'title': 'Test', 'status': 'active'}
    assert client.post('/api/v1/normatives', json=payload).status_code == 201
    payload['code'] = 'ГОСТ 29322–2014'
    assert client.post('/api/v1/normatives', json=payload).status_code == 409


def test_active_recheck_reconciles_pending_obsolete_finding(client, monkeypatch):
    class SelectAll:
        def complete_json(self, *, system_prompt, user_prompt):
            return {'selected': [item['id'] for item in json.loads(user_prompt)['candidates']]}
    monkeypatch.setattr('app.normatives.extraction.DeepSeekClient', SelectAll)
    project = client.post('/api/v1/projects', json={'name': 'Recheck'}).json()['id']
    document = upload(client, project, 'tu', ['1.1. Выполнить проект согласно ГОСТ 29322-2014.'])
    assert client.post(f'/api/v1/documents/{document}/extract-facts').status_code == 200
    normative = client.post('/api/v1/normatives', json={
        'code': 'ГОСТ 29322-2014', 'title': 'Test', 'status': 'obsolete',
    }).json()
    assert client.post(f'/api/v1/documents/{document}/check-normatives').status_code == 200
    assert client.patch(f"/api/v1/normatives/{normative['id']}", json={'status': 'active'}).status_code == 200
    assert client.post(f'/api/v1/documents/{document}/check-normatives').json()['active'] == 1
    findings = client.get(f'/api/v1/projects/{project}/findings').json()
    assert not any(item['type'] == 'obsolete_normative' and item['review_status'] == 'pending' for item in findings)


def test_unchecked_empty_project_cannot_be_approved(client):
    project = client.post('/api/v1/projects', json={'name': 'Unchecked'}).json()['id']
    assert client.post(f'/api/v1/projects/{project}/approve').status_code == 409


def test_partial_native_overlay_does_not_prevent_ocr(monkeypatch):
    buffer = BytesIO()
    Image.new('RGB', (595, 842), 'white').save(buffer, format='PNG')
    with fitz.open() as document:
        page = document.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=buffer.getvalue())
        for i in range(5):
            page.insert_text((40, 40 + 20 * i), 'Partial native overlay without engineering requirements.')
        pdf = document.tobytes()
    calls = []
    monkeypatch.setattr('app.documents.parser._ocr_lines', lambda *args: calls.append(1) or [])
    extract_blocks(pdf, '.pdf')
    assert calls


def test_pair_decisions_survive_a_b_a_and_confirmation_is_unambiguous(client):
    project = client.post('/api/v1/projects', json={'name': 'Pairs'}).json()['id']
    tu = upload(client, project, 'tu', ['1.1. Предусмотреть питание 24 В.'])
    a = upload(client, project, 'znp', ['1.1. Предусмотреть питание 24 В.'])
    b = upload(client, project, 'znp', ['1.1. Установить освещение.'])
    first = analyze(client, project, tu, a)['checked_requirements'][0]
    url = f'/api/v1/requirements/projects/{project}/confirm'
    assert client.post(url, json={'requirement_id': first['requirement']['id'],
                                 'verification_id': first['id'], 'status': 'fulfilled'}).status_code == 200
    second = analyze(client, project, tu, b)['checked_requirements'][0]
    assert second['id'] != first['id']
    assert not second['is_confirmed_by_human']
    assert client.post(url, json={'requirement_id': first['requirement']['id'],
                                 'status': 'missing'}).status_code == 409
    again = analyze(client, project, tu, a)['checked_requirements'][0]
    assert again['id'] == first['id']
    assert again['status'] == 'fulfilled'
    for doc, expected in [(a, first['id']), (b, second['id'])]:
        cards = client.get(f'/api/v1/requirements/projects/{project}', params={
            'tu_document_id': tu, 'znp_document_id': doc}).json()['checked_requirements']
        assert [card['id'] for card in cards] == [expected]


def test_empty_reanalysis_rejected_before_mutating_existing_results(client, monkeypatch):
    project = client.post('/api/v1/projects', json={'name': 'Empty reanalysis'}).json()['id']
    tu = upload(client, project, 'tu', ['1.1. Предусмотреть питание 24 В.'])
    znp = upload(client, project, 'znp', ['1.1. Предусмотреть питание 24 В.'])
    original = analyze(client, project, tu, znp)
    monkeypatch.setattr('app.requirements.router._extract_clauses_from_doc', lambda *args: [])
    response = client.post('/api/v1/requirements/analyze', json={
        'project_id': project, 'tu_document_id': tu, 'znp_document_id': znp})
    assert response.status_code == 422
    cards = client.get(f'/api/v1/requirements/projects/{project}').json()['checked_requirements']
    assert [c['id'] for c in cards] == [c['id'] for c in original['checked_requirements']]


def test_missing_human_verdict_without_znp_quote_is_pair_scoped(client):
    project = client.post('/api/v1/projects', json={'name': 'No match pair'}).json()['id']
    tu = upload(client, project, 'tu', ['1.1. Предусмотреть резервное питание 24 В.'])
    a = upload(client, project, 'znp', ['1.1. Установить наружное освещение.'])
    b = upload(client, project, 'znp', ['1.1. Предусмотреть резервное питание 24 В.'])
    first = analyze(client, project, tu, a)['checked_requirements'][0]
    assert first['znp_fragment_id'] is None
    assert client.post(f'/api/v1/requirements/projects/{project}/confirm', json={
        'requirement_id': first['requirement']['id'], 'verification_id': first['id'],
        'status': 'missing'}).status_code == 200
    second = analyze(client, project, tu, b)['checked_requirements'][0]
    assert not second['is_confirmed_by_human']
    old = analyze(client, project, tu, a)['checked_requirements'][0]
    assert old['is_confirmed_by_human'] and old['status'] == 'missing'


def test_one_manual_confirmation_does_not_complete_two_requirement_analysis(client):
    project = client.post('/api/v1/projects', json={'name': 'Partial manual'}).json()['id']
    tu = upload(client, project, 'tu', ['1.1. Предусмотреть питание 24 В.', '1.2. Установить освещение.'])
    znp = upload(client, project, 'znp', ['1.1. Предусмотреть питание 24 В.'])
    requirement = client.post(f'/api/v1/requirements/projects/{project}/tu-requirements', json={
        'document_id': tu, 'clause_number': '1.1', 'text': 'Предусмотреть питание 24 В.', 'category': 'other'}).json()
    assert client.post(f'/api/v1/requirements/projects/{project}/confirm', json={
        'requirement_id': requirement['id'], 'znp_document_id': znp, 'status': 'fulfilled'}).status_code == 200
    assert client.post(f'/api/v1/projects/{project}/approve').status_code == 409


def test_no_match_finding_exposes_pair_identity(client):
    project = client.post('/api/v1/projects', json={'name': 'Finding pair'}).json()['id']
    tu = upload(client, project, 'tu', ['1.1. Предусмотреть питание 24 В.'])
    znp = upload(client, project, 'znp', ['1.1. Установить освещение.'])
    analyze(client, project, tu, znp)
    findings = client.get(f'/api/v1/projects/{project}/findings').json()
    assert findings[0]['evidence'][0]['znp_document_id'] == znp


def test_legacy_registry_code_stays_accessible(client):
    from app.core.db import get_db
    from app.core.models import Normative
    from app.main import app
    sessions = app.dependency_overrides[get_db]()
    db = next(sessions)
    try:
        db.add(Normative(code='ГОСТ 29322–2014', title='Legacy', status='active'))
        db.commit()
    finally:
        sessions.close()
    assert client.get('/api/v1/normatives/ГОСТ 29322-2014').status_code == 200
    assert client.post('/api/v1/normatives', json={
        'code': 'ГОСТ 29322-2014', 'title': 'Duplicate', 'status': 'active'}).status_code == 409
