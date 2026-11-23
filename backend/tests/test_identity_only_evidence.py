"""An authentic organization heading does not prove an engineering obligation."""
from collections import Counter

import pytest

from app.requirements.schemas import TURequirement, ZNPClause, RequirementCategory
from app.requirements.znp_first import ZnPFirstAgent


IDENTITY = 'АО «Энергия»\nФилиал АО «Энергия» — Северная ГРЭС'


@pytest.mark.parametrize("text,quote", [
    ('2.1.1. Филиал АО «Энергия» — Северная ГРЭС расположен по адресу: '
     'посёлок Северный, ул. Промышленная, д. 1.',
     '2.1.1. Филиал АО «Энергия» — Северная ГРЭС расположен по адресу'),
    ('14.1. Настоящие ТУ составлены в двух экземплярах.\n'
     'Заявитель | АО «Энергия» Филиал АО «Энергия» — Северная ГРЭС',
     'АО «Энергия» Филиал АО «Энергия» — Северная ГРЭС'),
])
def test_identity_heading_cannot_support_address_or_contract(text, quote):
    tu = TURequirement(id="tu", clause_number="1", category=RequirementCategory.other,
                       text=text, sources=[dict(fragment_id="t", text=text)])
    atom = ZNPClause(id="z", clause_number="title", text=IDENTITY,
                     sources=[dict(fragment_id="z", text=IDENTITY)])
    proposal = dict(relation="supports", confidence=.95, tu_quote=quote,
                    znp_quote='Филиал АО «Энергия» — Северная ГРЭС')
    rejected = Counter()
    link = ZnPFirstAgent._validated_link(atom, tu, proposal, .9, rejected)
    assert link is None
    assert rejected["insufficient_obligation_evidence"] == 1


@pytest.mark.parametrize("text", [
    "Максимальная токовая защита блока и линий 110 кВ.",
    'АО «Энергия» должно предусмотреть защиту трансформатора.',
    'Филиал АО «Энергия» — Северная ГРЭС расположен по адресу: ул. Мира, д. 1.',
    'Предусмотреть мощность 236 МВт на филиале АО «Энергия».',
])
def test_substantive_statement_with_entity_keeps_support(text):
    tu = TURequirement(id="tu", clause_number="1", category=RequirementCategory.other,
                       text=text, sources=[dict(fragment_id="t", text=text)])
    atom = ZNPClause(id="z", clause_number="1", text=text,
                     sources=[dict(fragment_id="z", text=text)])
    link = ZnPFirstAgent._validated_link(atom, tu, dict(
        relation="supports", confidence=.95, tu_quote=text, znp_quote=text), .9)
    assert link["relation"] == "supports"


def test_full_atom_does_not_rescue_identity_only_quote():
    text = 'АО «Энергия»\nПредусмотреть защиту трансформатора.'
    tu = TURequirement(id="tu", clause_number="1", category=RequirementCategory.other,
                       text=text, sources=[dict(fragment_id="t", text=text)])
    atom = ZNPClause(id="z", clause_number="1", text=text,
                     sources=[dict(fragment_id="z", text=text)])
    link = ZnPFirstAgent._validated_link(atom, tu, dict(
        relation="supports", confidence=.95, tu_quote='АО «Энергия»',
        znp_quote='АО «Энергия»'), .9)
    assert link is None


def test_rejected_identity_evidence_stays_needs_review():
    import json
    from app.ai.embeddings import EmbeddingService

    text = 'Филиал АО «Энергия» — Северная ГРЭС расположен по адресу: ул. Мира, д. 1.'
    tu = TURequirement(id="tu", clause_number="1", category=RequirementCategory.other,
                       text=text, sources=[dict(fragment_id="t", text=text)])
    atom = ZNPClause(id="z", clause_number="1", text=IDENTITY,
                     sources=[dict(fragment_id="z", text=IDENTITY)])
    class Model:
        def complete_json(self, *, system_prompt, user_prompt):
            if "znp_atom" in json.loads(user_prompt):
                return dict(links=[dict(requirement_id="tu", relation="supports", confidence=.95,
                                       tu_quote=text, znp_quote='Филиал АО «Энергия» — Северная ГРЭС')],
                            search_more=False)
            return dict(links=[], covered=False, conflicts=[], uncertain_ids=[])

    agent = ZnPFirstAgent(ai_client=Model(), embedder=EmbeddingService(enabled=False),
                          targeted_enabled=False)
    result = agent.verify_all([tu], [atom])
    assert result.checked_requirements[0].status.value == "needs_review"


def test_real_saved_v4_b3_replay_does_not_support_obligations():
    import json
    from pathlib import Path

    root = Path("/large")
    if not (root / "subset-run-1/private/V4-b3.json").is_file():
        pytest.skip("Private frozen V4-b3 fixture unavailable")
    row = next(x for x in json.loads((root / "dataset.private.json").read_text())
               if x["id"] == "V4")
    raw = json.loads((root / "subset-run-1/private/V4-b3.json").read_text())
    proposals = json.loads(json.loads(raw["response"])["choices"][0]["message"]["content"])["links"]
    cfg = row["configs"]["3"]
    candidates = {c["requirement_id"]: c for c in cfg["candidates"] + cfg["context"]}
    atom = ZNPClause.model_validate(row["z"])
    assert len(proposals) == 3
    for proposal in proposals:
        c = candidates[proposal["requirement_id"]]
        tu = TURequirement(id=c["requirement_id"], clause_number=c["clause_number"],
                           category=RequirementCategory.other, text=c["text"],
                           sources=c["sources"], warnings=c.get("warnings", []))
        link = ZnPFirstAgent._validated_link(atom, tu, proposal, 0)
        assert link is None


@pytest.mark.parametrize("relation", ["supports", "partial"])
@pytest.mark.parametrize("ending", ["", "."])
def test_genuine_organization_identification_is_allowed(relation, ending):
    text = 'Наименование организации: АО «Энергия»' + ending
    tu = TURequirement(id="tu", clause_number="1", category=RequirementCategory.other,
                       text=text, sources=[dict(fragment_id="t", text=text)])
    atom = ZNPClause(id="z", clause_number="1", text='АО «Энергия»',
                     sources=[dict(fragment_id="z", text='АО «Энергия»')])
    link = ZnPFirstAgent._validated_link(atom, tu, dict(
        relation=relation, confidence=.95, tu_quote='АО «Энергия»',
        znp_quote='АО «Энергия»'), .9)
    assert link["relation"] == relation


@pytest.mark.parametrize("quote", [
    'АО «Энергия».',
    'АО «Энергия»\nФилиал АО «Энергия» — Северная ГРЭС',
])
def test_punctuation_or_multiple_entity_names_cannot_bypass_obligation_guard(quote):
    text = 'Два экземпляра ТУ. ' + quote
    tu = TURequirement(id="t", clause_number="14", category=RequirementCategory.other,
                       text=text, sources=[dict(fragment_id="t", text=text)])
    atom = ZNPClause(id="z", clause_number="title", text=quote,
                     sources=[dict(fragment_id="z", text=quote)])
    link = ZnPFirstAgent._validated_link(atom, tu, dict(
        relation="partial", confidence=.95, tu_quote=quote, znp_quote=quote), .9)
    assert link is None
