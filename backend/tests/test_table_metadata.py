"""Column labels and pure table-navigation prose are structural context."""
from collections import Counter

from app.core.models import Fragment
from app.requirements.atomization import atomize_fragments
from app.requirements.schemas import TURequirement, ZNPClause, RequirementCategory
from app.requirements.source_coverage import source_coverage_ledger
from app.requirements.znp_first import ZnPFirstAgent


def row(i, text, *, container="table-1", kind="table_row"):
    return Fragment(id=f"f{i}", document_id="d", page=1, position=i,
                    text=text, block_type=kind, container_id=container,
                    section_path="2. Параметры")


def test_parameter_table_header_and_pointer_are_context_not_requirements():
    fragments = [row(0, "Основные технические параметры установки приведены в таблице 1.",
                     container=None, kind="paragraph"),
                 row(1, "№ | Параметр | Единица измерения | Значение"),
                 row(2, "2 | Номинальная активная мощность | МВт | 236,0"),
                 row(3, "7 | Номинальное напряжение | кВ | 110")]
    atoms = atomize_fragments("d", fragments)
    assert len(atoms) == 2
    for atom in atoms:
        assert {s.fragment_id for s in atom.context_sources} >= {"f0", "f1"}
        assert len(atom.sources) == 1
        assert atom.sources[0].fragment_id in {"f2", "f3"}
        for source in atom.sources + atom.context_sources:
            assert fragments[int(source.fragment_id[1:])].text[source.start_offset:source.end_offset] == source.text
    ledger = source_coverage_ledger(fragments, atoms)
    assert ledger["complete"]
    assert ledger["by_role"]["context_only"] == 2


def test_function_table_retains_real_functions_and_header_provenance():
    fragments = [row(0, "№ | Функция РЗА / ПА | Нормативный документ")]
    for i, name in enumerate(["МТЗ блока 110 кВ", "ЗМН", "УРОВ", "АПВ"], 1):
        fragments.append(row(i, f"{i} | {name} | Приказ Минэнерго № 546"))
    atoms = atomize_fragments("d", fragments)
    assert len(atoms) == 4
    assert all("f0" in {s.fragment_id for s in a.context_sources} for a in atoms)
    assert all("f0" not in {s.fragment_id for s in a.sources} for a in atoms)


def test_real_obligation_mentioning_table_or_column_words_is_not_filtered():
    fragments = [row(0, "1. Параметры должны соответствовать значениям таблицы 1.",
                     container=None, kind="paragraph"),
                 row(1, "1 | Показатель мощности | МВт | 236"),
                 row(2, "2 | Предусмотреть функцию АПВ | Нормативный документ ГОСТ 123-2020")]
    atoms = atomize_fragments("d", fragments)
    assert {s.fragment_id for a in atoms for s in a.sources} == {"f0", "f1", "f2"}


def test_header_context_cannot_cross_table_boundary():
    fragments = [row(0, "№ | Функция РЗА / ПА | Нормативный документ", container="A"),
                 row(1, "1 | МТЗ | Приказ № 546", container="A"),
                 row(2, "1 | Мощность | МВт | 236", container="B")]
    atoms = atomize_fragments("d", fragments)
    other = next(a for a in atoms if any(s.fragment_id == "f2" for s in a.sources))
    assert "f0" not in {s.fragment_id for s in other.context_sources}


def test_legacy_stored_header_cannot_support_concrete_function():
    header = "№ | Функция РЗА / ПА | Нормативный документ"
    tu = TURequirement(id="t", clause_number="header", category=RequirementCategory.protection,
                       text=header, sources=[dict(fragment_id="t", text=header)])
    atom = ZNPClause(id="z", clause_number="1", text="Предусмотреть АПВ по нормативному документу.")
    rejected = Counter()
    link = ZnPFirstAgent._validated_link(atom, tu, dict(relation="supports", confidence=.99,
        tu_quote=header, znp_quote=atom.text), .9, rejected)
    assert link is None
    assert rejected["structural_metadata_not_obligation"] == 1


def test_unknown_source_coverage_never_auto_fulfills():
    from app.requirements.schemas import CheckRequirementsResponse, RequirementCheckItem, RequirementStatus
    from app.requirements.source_coverage import apply_source_coverage_guard
    tu = TURequirement(id="t", clause_number="1", category=RequirementCategory.other,
                       text="Предусмотреть защиту.")
    response = CheckRequirementsResponse(checked_requirements=[RequirementCheckItem(
        id="t", requirement=tu, status=RequirementStatus.fulfilled,
        confidence_score=.99)], stats={"fulfilled": 1, "total": 1})
    apply_source_coverage_guard(response, None)
    assert response.checked_requirements[0].status == RequirementStatus.needs_review
    assert response.stats["fulfilled"] == 0
    assert response.trace_info["source_coverage"]["status"] == "unknown"
    assert response.trace_info["source_coverage"]["complete"] is False


def test_chunk_only_project_apis_cannot_bypass_unknown_coverage(client, monkeypatch):
    from app.core.db import get_db
    from app.core.models import Document, DocumentKind, Chunk
    from app.main import app
    from app.requirements.schemas import CheckRequirementsResponse, RequirementCheckItem, RequirementStatus
    pid = client.post('/api/v1/projects', json={'name': 'Legacy chunks only'}).json()['id']
    docs = {}
    with next(app.dependency_overrides[get_db]()) as db:
        for kind in ['tu', 'znp']:
            doc = Document(project_id=pid, kind=DocumentKind(kind), filename=kind + '.docx',
                           stored_path='/not-read.docx', mime_type='application/octet-stream')
            db.add(doc)
            db.flush()
            docs[kind] = doc.id
            db.add(Chunk(document_id=doc.id, position=0, page_start=1, page_end=1,
                         text='Предусмотреть защиту трансформатора.'))
        db.commit()
    def optimistic(self, requirements, atoms, *args, **kwargs):
        return CheckRequirementsResponse(checked_requirements=[RequirementCheckItem(
            id=r.id, requirement=r, status=RequirementStatus.fulfilled, confidence_score=.99)
            for r in requirements], stats={'fulfilled': len(requirements), 'total': len(requirements)})
    monkeypatch.setattr(ZnPFirstAgent, 'verify_all', optimistic)
    root = f'/api/v1/projects/{pid}/comparisons'
    pair = client.post(root, json={'pairs': [{'tu_document_id': docs['tu'],
                                            'znp_document_id': docs['znp']}]}).json()[0]['id']
    first = client.post(f'{root}/{pair}/runs')
    second = client.post('/api/v1/requirements/analyze', json={'project_id': pid})
    assert first.status_code == 201 and second.status_code == 200
    for result in [first.json()['result'], second.json()]:
        assert result['stats']['fulfilled'] == 0
        assert result['trace_info']['source_coverage']['status'] == 'unknown'
