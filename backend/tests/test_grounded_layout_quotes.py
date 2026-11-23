"""Layout wrapping is allowed only with server-side fragment provenance."""
from collections import Counter
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app.requirements.atomization import AtomSource
from app.requirements.schemas import TURequirement, ZNPClause, RequirementCategory
from app.requirements.tools import tool_validate_quotes
from app.requirements.znp_first import ZnPFirstAgent


def case(text="Предусмотреть защиту\nтрансформатора от\nперегрузки."):
    source = asdict(AtomSource("f", 1, 7, 7 + len(text), text))
    fragment = SimpleNamespace(id="f", document_id="zdoc", page=1,
                               text="prefix " + text + " suffix")
    tu = TURequirement(id="tu", clause_number="1", category=RequirementCategory.protection,
                       text=text, sources=[{"fragment_id": "t", "text": text}])
    atom = ZNPClause(id="z", clause_number="2", text=text, sources=[source])
    proposal = dict(relation="supports", tu_quote=text, znp_quote=text, confidence=.95)
    return tu, atom, proposal, {"f": fragment}


def validate(tu, atom, proposal, fragments, owner="zdoc"):
    return ZnPFirstAgent._validated_link(atom, tu, proposal, 1, Counter(),
                                         fragments_by_id=fragments, znp_document_id=owner)


def test_wrapped_quote_requires_exact_source_span_and_owner():
    tu, atom, proposal, fragments = case()
    assert not tool_validate_quotes(tu.text, proposal["tu_quote"], atom.text,
                                    proposal["znp_quote"])["znp_quote_atomic"]
    assert validate(tu, atom, proposal, fragments)["relation"] == "supports"


@pytest.mark.parametrize("broken", ["owner", "identity", "span", "text", "missing", "unverified"])
def test_unproven_layout_quote_stays_rejected(broken):
    tu, atom, proposal, fragments = case()
    owner = "zdoc"
    if broken == "owner":
        owner = "other-document"
    if broken == "identity":
        fragments["f"].id = "different-fragment"
    if broken == "span":
        atom.sources[0]["end_offset"] -= 1
    if broken == "text":
        fragments["f"].text = "different raw fragment"
    if broken == "missing":
        fragments = {}
    if broken == "unverified":
        atom.warnings = ["table_structure_unverified"]
    assert validate(tu, atom, proposal, fragments, owner) is None


@pytest.mark.parametrize("same_fragment", [False, True])
def test_sources_and_adjacent_cell_spans_cannot_be_glued(same_fragment):
    tu, atom, proposal, fragments = case()
    text = atom.text
    split = text.index("\n")
    if same_fragment:
        atom.sources = [dict(atom.sources[0], end_offset=7+split, text=text[:split]),
                        dict(atom.sources[0], start_offset=7+split+1, text=text[split+1:])]
    else:
        atom.sources = [dict(atom.sources[0], end_offset=7+split, text=text[:split]),
                        dict(fragment_id="f2", page=1, start_offset=0,
                             end_offset=len(text[split+1:]), text=text[split+1:])]
        fragments["f2"] = SimpleNamespace(id="f2", document_id="zdoc", page=1,
                                           text=text[split+1:])
    assert validate(tu, atom, proposal, fragments) is None


@pytest.mark.parametrize("text", [
    "1. Установить защиту.\n2. Установить счётчик.\n3. Выполнить учёт.",
    "- Установить защиту.\n- Установить счётчик.\n- Выполнить учёт.",
    "Защитить трансформатор.\n\nУстановить счётчик.\nВыполнить учёт.",
    "Защитить трансформатор.\nУстановить счётчик.\nВыполнить учёт.",
    "защитить трансформатор.\nустановить счётчик.\nвыполнить учёт.",
    "Защита " + "трансформатора\n" * 60,
])
def test_lists_paragraph_glue_and_oversize_remain_forbidden(text):
    tu, atom, proposal, fragments = case(text)
    assert validate(tu, atom, proposal, fragments) is None


def test_fabricated_quote_remains_rejected():
    tu, atom, proposal, fragments = case()
    proposal["znp_quote"] += " Несуществующая цитата."
    assert validate(tu, atom, proposal, fragments) is None


def test_layout_quote_with_two_possible_source_spans_is_not_certified():
    tu, atom, proposal, fragments = case()
    duplicate = SimpleNamespace(**vars(fragments["f"]))
    duplicate.id = "other"
    fragments["other"] = duplicate
    atom.sources.append(dict(atom.sources[0], fragment_id="other"))
    assert validate(tu, atom, proposal, fragments) is None


def test_layout_permission_does_not_override_support_guards():
    tu, atom, proposal, fragments = case()
    tu.warnings = ["table_structure_unverified"]
    proposal["confidence"] = .55
    link = validate(tu, atom, proposal, fragments)
    assert link["relation"] == "partial"
    assert "table_structure_unverified" in link["warnings"]


def test_saved_real_p17_has_grounded_quote_but_remains_partial():
    import json
    from pathlib import Path
    from uuid import uuid5, NAMESPACE_URL
    from app.ai.diagnostics import sha
    data = Path("/large/dataset.private.json")
    original = Path("/inputs/koktal_znp.pdf")
    if not data.is_file() or not original.is_file():
        pytest.skip("Private original P17 fixture unavailable")
    rows = json.loads(data.read_text())
    row = next(row for row in rows if row["id"] == "P17")
    raw = json.loads(Path("/large/subset-run-1/private/P17-b1.json").read_text())
    reply = json.loads(json.loads(raw["response"])["choices"][0]["message"]["content"])
    proposal = reply["links"][0]
    candidate = row["configs"]["1"]["candidates"][0]
    tu = TURequirement(id=candidate["requirement_id"], clause_number=candidate["clause_number"],
                       category=RequirementCategory.other, text=candidate["text"],
                       sources=candidate["sources"], warnings=candidate["warnings"])
    atom = ZNPClause.model_validate(row["z"])
    pool = json.loads(Path("/large/pools.private.json").read_text())["pdf"]
    fragments = {f["id"]: f for f in pool["fragments"]}
    owner = str(uuid5(NAMESPACE_URL, f"diagnostic:znp:{sha(original.read_bytes())}"))
    assert proposal["znp_quote"].count("\n") == 3
    link = validate(tu, atom, proposal, fragments, owner)
    assert link["relation"] == "partial"
    assert link["quote"] == proposal["znp_quote"]
    assert "table_structure_unverified" in link["warnings"]
