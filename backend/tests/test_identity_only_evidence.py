"""An authentic organization heading does not prove an engineering obligation."""
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
    link = ZnPFirstAgent._validated_link(atom, tu, proposal, .9)
    assert link is not None
    assert link["relation"] == "partial"
    assert "identity_only_evidence" in link["warnings"]


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
    assert link["relation"] == "partial"


def test_identity_warning_prevents_positive_coverage_even_if_model_agrees():
    from app.requirements.coverage import assess_coverage, build_obligations

    text = 'Филиал АО «Энергия» — Северная ГРЭС расположен по адресу: ул. Мира, д. 1.'
    tu = TURequirement(id="tu", clause_number="1", category=RequirementCategory.other,
                       text=text, sources=[dict(fragment_id="t", text=text)])
    atom = ZNPClause(id="z", clause_number="1", text=IDENTITY,
                     sources=[dict(fragment_id="z", text=IDENTITY)])
    link = ZnPFirstAgent._validated_link(atom, tu, dict(
        relation="supports", confidence=.95, tu_quote=text,
        znp_quote='Филиал АО «Энергия» — Северная ГРЭС'), .9)

    class PositiveCoverage:
        def complete_json(self, **kwargs):
            return dict(covered=True, object_consistent=True,
                        evidence_ids=[link["link_id"]])

    result = assess_coverage(tu, build_obligations(tu)[0], [link], PositiveCoverage())
    assert result["status"] == "needs_review"


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
        assert link["relation"] == "partial"
        assert "identity_only_evidence" in link["warnings"]
