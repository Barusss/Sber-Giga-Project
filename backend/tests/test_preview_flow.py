"""Exercise the same queued comparison route as the 10x10 button."""
import json
from io import BytesIO

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.db import get_db
from app.main import app
from app.documents.parser import ExtractedBlock
import app.operations as operations


@pytest.fixture
def flow(client, monkeypatch):
    # Reuse the API fixture's in-memory database in the worker. The synchronous
    # submit only changes scheduling; it calls the real worker and comparison.
    session_factory = next(app.dependency_overrides[get_db]()).bind
    Session = sessionmaker(bind=session_factory, expire_on_commit=False)
    monkeypatch.setattr(operations, "SessionLocal", Session)
    monkeypatch.setattr(operations._workers, "submit", lambda callback, *args: callback(*args))

    def blocks(data, suffix):
        return [ExtractedBlock(1, i, f"{i+1}. Для КЛ-{i+1} предусмотреть мощность 150 кВт.")
                for i in range(12)]

    monkeypatch.setattr("app.documents.router.extract_blocks", blocks)

    def model(self, *, system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        if "clause_text" in payload:
            return {"atoms": []}
        if "znp_candidates" in payload:
            return {"links": []}
        if "znp_atom" in payload:
            atom = payload["znp_atom"]
            candidate = next((candidate for candidate in payload["tu_candidates"]
                              if atom["text"].split("КЛ-")[1].split()[0] ==
                              candidate["text"].split("КЛ-")[1].split()[0]), None)
            if candidate is None:
                return {"links": [], "search_more": False}
            return {"links": [{"requirement_id": candidate["requirement_id"],
                              "relation": "supports", "tu_quote": candidate["text"],
                              "znp_quote": atom["text"], "confidence": 0.95}],
                    "search_more": False}
        if "obligation" in payload:
            return {"covered": True, "object_consistent": True,
                    "evidence_ids": [item["link_id"] for item in payload["evidence"]]}
        if "candidates" in payload:
            return {"conflicts": [], "uncertain_ids": []}
        raise AssertionError("unexpected DeepSeek phase")

    monkeypatch.setattr("app.ai.deepseek.DeepSeekClient.complete_json", model)
    project = client.post("/api/v1/projects", json={"name": "Control"}).json()["id"]
    docs = []
    for kind in ("tu", "znp"):
        response = client.post(f"/api/v1/projects/{project}/documents", data={"kind": kind},
                               files={"file": (kind + ".pdf", BytesIO((kind + "-flow").encode()),
                                               "application/pdf")})
        assert response.status_code == 201, response.text
        docs.append(response.json()["id"])
    pair = client.post(f"/api/v1/projects/{project}/comparisons", json={"pairs": [{
        "tu_document_id": docs[0], "znp_document_id": docs[1]}]})
    assert pair.status_code == 201, pair.text
    return client, project, pair.json()[0]["id"]


def test_queued_preview_persists_and_displays_ten_links(flow):
    client, project, pair = flow
    response = client.post(f"/api/v1/projects/{project}/operations", json={
        "kind": "comparison", "pair_ids": [pair], "test_mode": True})
    assert response.status_code == 202, response.text
    job = client.get(f"/api/v1/projects/{project}/operations/{response.json()['id']}").json()
    assert job["status"] == "completed", job
    run_id = job["result"]["runs"][0]["run_id"]
    saved = client.get(f"/api/v1/projects/{project}/comparisons/{pair}/runs/{run_id}").json()
    result = saved["result"]
    assert result["trace_info"]["validated_links"] == 10, result["trace_info"]
    assert result["stats"]["fulfilled"] == 10, result
    assert all(len(atom["links"]) == 1 for atom in result["trace_info"]["znp_atom_results"])
    for atom in result["trace_info"]["znp_atom_results"]:
        assert atom["outcome"] == "accepted_links"
        assert len(atom["candidates"]) == 10
        assert len({candidate["requirement_id"] for candidate in atom["candidates"]}) == 10


def test_queued_preview_distinguishes_provider_error_from_empty_links(flow, monkeypatch):
    from app.ai.deepseek import DeepSeekError
    client, project, pair = flow

    def unavailable(self, *, system_prompt, user_prompt):
        raise DeepSeekError("Cannot connect to DeepSeek API.")

    monkeypatch.setattr("app.ai.deepseek.DeepSeekClient.complete_json", unavailable)
    response = client.post(f"/api/v1/projects/{project}/operations", json={
        "kind": "comparison", "pair_ids": [pair], "test_mode": True})
    assert response.status_code == 202, response.text
    job = client.get(f"/api/v1/projects/{project}/operations/{response.json()['id']}").json()
    assert job["status"] == "failed", job
    assert "0 of 10 ZnP atoms checked" in job["error"]
    runs = client.get(f"/api/v1/projects/{project}/comparisons/{pair}/runs").json()
    assert runs == []


def test_queued_preview_explains_rejected_source_quote(flow, monkeypatch):
    client, project, pair = flow

    def invent_quote(self, *, system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        if "znp_atom" in payload:
            return {"links": [{"requirement_id": payload["tu_candidates"][0]["requirement_id"],
                              "relation": "supports", "tu_quote": "выдуманная цитата",
                              "znp_quote": payload["znp_atom"]["text"], "confidence": 0.99}],
                    "search_more": False}
        if "obligation" in payload:
            return {"covered": False, "object_consistent": False, "evidence_ids": []}
        if "candidates" in payload:
            return {"conflicts": [], "uncertain_ids": []}
        return {"atoms": []}

    monkeypatch.setattr("app.ai.deepseek.DeepSeekClient.complete_json", invent_quote)
    response = client.post(f"/api/v1/projects/{project}/operations", json={
        "kind": "comparison", "pair_ids": [pair], "test_mode": True})
    job = client.get(f"/api/v1/projects/{project}/operations/{response.json()['id']}").json()
    assert job["status"] == "completed", job
    result = client.get(f"/api/v1/projects/{project}/comparisons/{pair}/runs/"
                        + job["result"]["runs"][0]["run_id"]).json()["result"]
    first = result["trace_info"]["znp_atom_results"][0]
    assert first["outcome"] == "rejected_links"
    assert first["proposed_links"] >= 1 and first["validated_links"] == 0
    assert first["rejected_links"]["tu_quote_not_in_clause"] >= 1
    assert result["stats"]["fulfilled"] == 0


def test_split_of_tenth_source_clause_is_not_silently_discarded(flow, monkeypatch):
    client, _, _ = flow
    last = ("Для КЛ-10 предусмотреть алюминиевый кабель "
            "сечением не менее 120 мм² на напряжение 10 кВ.")

    def blocks(data, suffix):
        texts = [f"Для КЛ-{i} предусмотреть мощность 150 кВт." for i in range(1, 10)]
        texts.append(last)
        return [ExtractedBlock(1, i, f"{i+1}. {text}") for i, text in enumerate(texts)]

    monkeypatch.setattr("app.documents.router.extract_blocks", blocks)

    def model(self, *, system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        if "clause_text" in payload:
            return {"atoms": [
                {"quote": "алюминиевый кабель", "context_quotes": ["10. Для КЛ-10 предусмотреть"]},
                {"quote": "сечением не менее 120 мм²", "context_quotes": ["10. Для КЛ-10 предусмотреть"]},
                {"quote": "на напряжение 10 кВ.", "context_quotes": ["10. Для КЛ-10 предусмотреть"]},
            ]}
        if "znp_atom" in payload:
            return {"links": [], "search_more": False}
        if "obligation" in payload:
            return {"covered": False, "evidence_ids": [], "object_consistent": False}
        return {"conflicts": [], "uncertain_ids": []}

    monkeypatch.setattr("app.ai.deepseek.DeepSeekClient.complete_json", model)
    project = client.post("/api/v1/projects", json={"name": "Real split"}).json()["id"]
    docs = []
    for kind in ("tu", "znp"):
        response = client.post(f"/api/v1/projects/{project}/documents", data={"kind": kind},
                               files={"file": (kind + ".pdf", BytesIO((kind + "-split").encode()),
                                               "application/pdf")})
        assert response.status_code == 201, response.text
        docs.append(response.json()["id"])
    pairs = client.post(f"/api/v1/projects/{project}/comparisons", json={"pairs": [{
        "tu_document_id": docs[0], "znp_document_id": docs[1]}]})
    assert pairs.status_code == 201, pairs.text
    pair = pairs.json()[0]["id"]
    response = client.post(f"/api/v1/projects/{project}/operations", json={
        "kind": "comparison", "pair_ids": [pair], "test_mode": True})
    assert response.status_code == 202, response.text
    job = client.get(f"/api/v1/projects/{project}/operations/{response.json()['id']}").json()
    assert job["status"] == "completed", job
    saved = client.get(f"/api/v1/projects/{project}/comparisons/{pair}/runs/"
                       + job["result"]["runs"][0]["run_id"]).json()["result"]
    assert saved["trace_info"]["atomization"]["clauses_split"] == 1
    assert saved["trace_info"]["test_mode"]["znp_atoms_after_split"] == 12
    assert len(saved["trace_info"]["znp_atom_results"]) == 12
    assert "напряжение 10 кВ" in saved["trace_info"]["znp_atom_results"][-1]["text"]
    assert all(atom["model_checked"] for atom in saved["trace_info"]["znp_atom_results"])


def test_targeted_match_appears_in_atom_diagnostic():
    from test_znp_p0 import GroundedAI, agent, req, znp

    result = agent(GroundedAI(only_targeted=True)).verify_all(
        [req("1.1. Предусмотреть мощность 150 кВт.")],
        [znp(1, "2.1. Предусмотреть мощность 150 кВт.")],
    )
    atom = result.trace_info["znp_atom_results"][0]
    assert atom["outcome"] == "accepted_links"
    assert atom["validated_links"] == 1
    assert atom["proposed_links"] == 1


def test_targeted_error_is_not_reported_as_empty_model_answer():
    from app.ai.deepseek import DeepSeekError
    from test_znp_p0 import agent, req, znp

    class TargetedError:
        def complete_json(self, *, system_prompt, user_prompt):
            payload = json.loads(user_prompt)
            if "znp_candidates" in payload:
                raise DeepSeekError("Cannot connect to DeepSeek API.")
            if "znp_atom" in payload:
                if payload.get("phase") == "targeted":
                    raise DeepSeekError("Cannot connect to DeepSeek API.")
                return {"links": [], "search_more": False}
            if "obligation" in payload:
                return {"covered": False, "evidence_ids": [], "object_consistent": False}
            return {"conflicts": [], "uncertain_ids": []}

    result = agent(TargetedError()).verify_all(
        [req("1.1. Предусмотреть мощность 150 кВт.")],
        [znp(1, "2.1. Предусмотреть мощность 150 кВт.")],
    )
    atom = result.trace_info["znp_atom_results"][0]
    assert atom["outcome"] == "targeted_error"
    assert "Cannot connect" in atom["error"]


def test_duplicate_model_link_has_rejection_reason():
    from test_znp_orientation import MatchModel, compare, requirement, znp

    class DuplicateModel(MatchModel):
        def complete_json(self, *, system_prompt, user_prompt):
            response = super().complete_json(system_prompt=system_prompt, user_prompt=user_prompt)
            if "links" in response and response["links"]:
                response["links"].append(dict(response["links"][0]))
            return response

    result = compare([requirement()], [znp()], DuplicateModel())
    atom = result.trace_info["znp_atom_results"][0]
    assert atom["proposed_links"] == 2
    assert atom["validated_links"] == 1
    assert atom["rejected_links"]["duplicate_link"] == 1


def test_independent_conflict_is_visible_from_znp_side():
    from test_znp_p0 import GroundedAI, agent, req, znp

    result = agent(GroundedAI()).verify_all(
        [req("1.1. Для КЛ-1 предусмотреть напряжение 10 кВ.")],
        [znp(1, "2.1. Для КЛ-1 предусмотреть напряжение 10 кВ."),
         znp(2, "2.2. Для КЛ-1 предусмотреть напряжение 6 кВ.")],
    )
    conflicting_atom = result.trace_info["znp_atom_results"][1]
    assert any(link["relation"] == "conflicts" for link in conflicting_atom["links"])
    assert conflicting_atom["independent_conflict_links"] == 1
    assert result.trace_info["independent_conflict_links"] == 1


def test_independent_audit_error_is_not_an_empty_answer():
    from app.ai.deepseek import DeepSeekError
    from test_znp_p0 import GroundedAI, agent, req, znp

    class AuditError(GroundedAI):
        def complete_json(self, *, system_prompt, user_prompt):
            payload = json.loads(user_prompt)
            if "candidates" in payload and "tu_candidates" not in payload:
                raise DeepSeekError("Cannot connect to DeepSeek API.")
            return super().complete_json(system_prompt=system_prompt, user_prompt=user_prompt)

    result = agent(AuditError()).verify_all(
        [req("1.1. Для КЛ-1 предусмотреть напряжение 10 кВ.")],
        [znp(1, "2.1. Для КЛ-1 предусмотреть напряжение 10 кВ."),
         znp(2, "2.2. Для КЛ-1 предусмотреть напряжение 6 кВ.")],
    )
    atom = result.trace_info["znp_atom_results"][1]
    assert atom["outcome"] == "audit_error"
    assert "Cannot connect" in atom["error"]
    assert result.trace_info["degraded"] is True
    assert result.checked_requirements[0].status.value == "needs_review"
    assert result.checked_requirements[0].validation_details["contradiction_audit"]["candidates_checked"] == 0


@pytest.mark.parametrize("scenario,tu_texts,znp_texts,expected", [
    ("none", ["Для КЛ-1 предусмотреть мощность 150 кВт."],
     ["Для КЛ-9 предусмотреть мощность 150 кВт."], {"links": 0, "status": "needs_review"}),
    ("partial", ["Для КЛ-1 предусмотреть мощность 150 кВт."],
     ["Для КЛ-1 предусмотреть мощность 150 кВт."], {"links": 1, "status": "needs_review"}),
    ("conflict", ["Для КЛ-1 предусмотреть мощность 150 кВт."],
     ["Для КЛ-1 предусмотреть мощность 200 кВт."], {"links": 1, "status": "needs_review"}),
    ("many", ["Для КЛ-1 предусмотреть мощность 150 кВт.",
              "Для КЛ-1 предусмотреть напряжение 10 кВ."],
     ["Для КЛ-1 предусмотреть мощность 150 кВт и напряжение 10 кВ.",
      "Для КЛ-1 предусмотреть мощность 150 кВт."], {"links": 3, "status": None}),
])
def test_control_cases_use_queued_preview(flow, monkeypatch, scenario, tu_texts, znp_texts, expected):
    client, _, _ = flow

    def blocks(data, suffix):
        texts = tu_texts if data == b"control-tu" else znp_texts
        return [ExtractedBlock(1, i, f"{i+1}. {text}") for i, text in enumerate(texts)]

    monkeypatch.setattr("app.documents.router.extract_blocks", blocks)

    def model(self, *, system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        if "clause_text" in payload:
            return {"atoms": []}
        if "znp_atom" in payload:
            atom = payload["znp_atom"]
            selected = []
            for candidate in payload["tu_candidates"]:
                if scenario == "none":
                    continue
                if scenario == "many" and atom["clause_number"] == "2" and candidate["clause_number"] == "2":
                    continue
                selected.append({
                    "requirement_id": candidate["requirement_id"],
                    "relation": ("partial" if scenario == "partial" else
                                 "conflicts" if scenario == "conflict" else "supports"),
                    "tu_quote": candidate["text"], "znp_quote": atom["text"],
                    "confidence": 0.95,
                })
            return {"links": selected, "search_more": False}
        if "obligation" in payload:
            return {"covered": scenario == "many", "object_consistent": True,
                    "evidence_ids": [item["link_id"] for item in payload["evidence"]]}
        if "candidates" in payload:
            return {"conflicts": [], "uncertain_ids": []}
        raise AssertionError("unexpected DeepSeek phase")

    monkeypatch.setattr("app.ai.deepseek.DeepSeekClient.complete_json", model)
    project = client.post("/api/v1/projects", json={"name": scenario}).json()["id"]
    docs = []
    for kind in ("tu", "znp"):
        response = client.post(f"/api/v1/projects/{project}/documents", data={"kind": kind},
                               files={"file": (kind + ".pdf", BytesIO(("control-" + kind).encode()),
                                               "application/pdf")})
        assert response.status_code == 201, response.text
        docs.append(response.json()["id"])
    pairs = client.post(f"/api/v1/projects/{project}/comparisons", json={"pairs": [{
        "tu_document_id": docs[0], "znp_document_id": docs[1]}]})
    assert pairs.status_code == 201, pairs.text
    pair = pairs.json()[0]["id"]
    response = client.post(f"/api/v1/projects/{project}/operations", json={
        "kind": "comparison", "pair_ids": [pair], "test_mode": True})
    assert response.status_code == 202, response.text
    job = client.get(f"/api/v1/projects/{project}/operations/{response.json()['id']}").json()
    assert job["status"] == "completed", job
    result = client.get(f"/api/v1/projects/{project}/comparisons/{pair}/runs/"
                        + job["result"]["runs"][0]["run_id"]).json()["result"]
    assert result["trace_info"]["validated_links"] == expected["links"], result["trace_info"]
    if expected["status"]:
        assert all(item["status"] == expected["status"] for item in result["checked_requirements"])
    if scenario == "none":
        assert result["trace_info"]["znp_atom_results"][0]["outcome"] == "no_links"
    if scenario in {"partial", "conflict"}:
        assert result["trace_info"]["znp_atom_results"][0]["links"][0]["relation"] == (
            "partial" if scenario == "partial" else "conflicts"
        )
        assert result["checked_requirements"][0]["validation_details"]["obligation_coverage"][0]["status"] != "fulfilled"
    if scenario == "many":
        assert sorted(len(atom["links"]) for atom in result["trace_info"]["znp_atom_results"]) == [1, 2]
        assert sorted(len(item["validation_details"]["matches"]) for item in result["checked_requirements"]) == [1, 2]
        assert all(item["status"] == "fulfilled" for item in result["checked_requirements"])
        assert all(all(row["status"] == "fulfilled" for row in item["validation_details"]["obligation_coverage"])
                   for item in result["checked_requirements"])


def test_queued_preview_reports_request_limit(flow, monkeypatch):
    from app.core.config import settings
    client, project, pair = flow
    monkeypatch.setattr(settings, "requirements_llm_max_calls", 1)
    response = client.post(f"/api/v1/projects/{project}/operations", json={
        "kind": "comparison", "pair_ids": [pair], "test_mode": True})
    job = client.get(f"/api/v1/projects/{project}/operations/{response.json()['id']}").json()
    assert job["status"] == "failed", job
    assert "1 of 10 ZnP atoms checked" in job["error"]
    runs = client.get(f"/api/v1/projects/{project}/comparisons/{pair}/runs").json()
    assert runs == []


def test_queued_preview_preserves_manual_verdict_and_findings(flow):
    client, project, pair = flow
    runs_url = f"/api/v1/projects/{project}/comparisons/{pair}/runs"
    full = client.post(runs_url)
    assert full.status_code == 201, full.text
    full_id = full.json()["run"]["id"]
    first_id = full.json()["result"]["checked_requirements"][0]["id"]
    confirmed = client.post(runs_url + f"/{full_id}/confirm", json={
        "verification_id": first_id, "status": "fulfilled", "comment": "Решение инженера",
    })
    assert confirmed.status_code == 200, confirmed.text
    verifications_url = f"/api/v1/requirements/projects/{project}/verifications"
    findings_url = f"/api/v1/projects/{project}/findings"
    before_verifications = client.get(verifications_url).json()
    before_findings = client.get(findings_url).json()
    before_full = client.get(runs_url + f"/{full_id}").json()
    assert before_full["result"]["checked_requirements"][0]["human_comment"] == "Решение инженера"

    response = client.post(f"/api/v1/projects/{project}/operations", json={
        "kind": "comparison", "pair_ids": [pair], "test_mode": True})
    job = client.get(f"/api/v1/projects/{project}/operations/{response.json()['id']}").json()
    assert job["status"] == "completed", job
    assert client.get(verifications_url).json() == before_verifications
    assert client.get(findings_url).json() == before_findings
    assert client.get(runs_url + f"/{full_id}").json() == before_full
    assert client.get(f"/api/v1/projects/{project}/comparisons/{pair}").json()["current_run_id"] == full_id
