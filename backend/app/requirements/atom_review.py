"""One-atom DeepSeek recheck from a saved report.

Creates separate review evidence. It does NOT rewrite the completed run,
human decisions, coverage verdicts or project findings.
"""
from __future__ import annotations

from collections import Counter

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.ai.deepseek import DeepSeekClient
from app.ai.embeddings import EmbeddingService
from app.core.models import (
    ComparisonAtomReview, ComparisonPair, ComparisonRun, Fragment,
)
from app.requirements.checkpoints import CheckpointedDeepSeek
from app.requirements.retrieval import RequirementRetriever
from app.requirements.schemas import TURequirement, ZNPClause
from app.requirements.znp_first import ZnPFirstAgent


def run_atom_recheck(db: Session, *, project_id: str, pair_id: str,
                     run_id: str, atom_id: str, job_id: str,
                     session_factory=None) -> dict:
    pair = db.get(ComparisonPair, pair_id)
    if pair is None or pair.project_id != project_id:
        raise HTTPException(404, "Comparison pair not found")
    run = db.get(ComparisonRun, run_id)
    if run is None or run.pair_id != pair_id:
        raise HTTPException(404, "Comparison run not found")
    trace = (run.result or {}).get("trace_info") or {}
    atom_rows = trace.get("znp_atom_results") or []
    row = next((x for x in atom_rows if x.get("atom_id") == atom_id), None)
    if row is None:
        raise HTTPException(404, "ZnP atom not found in this report")
    if not row.get("sources"):
        raise HTTPException(
            409, "Этот снимок не содержит проверяемых исходных фрагментов ЗнП. "
                 "Для повторной проверки используйте результат новой полной сверки.",
        )
    for source in row["sources"]:
        fragment = db.get(Fragment, source.get("fragment_id"))
        if fragment is None or fragment.document_id != pair.znp_document_id:
            raise HTTPException(409, "ZnP source fragment no longer matches this document")
        original = " ".join(fragment.text.split()).casefold()
        quote = " ".join(str(source.get("text") or "").split()).casefold()
        if not quote or quote not in original:
            raise HTTPException(409, "ZnP source text is not verifiable")

    atom = ZNPClause.model_validate({
        "id": row["atom_id"],
        "clause_number": row["clause_number"],
        "text": row["text"],
        "fragment_id": row.get("fragment_id"),
        "fragment_text": row.get("fragment_text"),
        "page": row.get("page"),
        "section_path": row.get("section_path"),
        "sources": row["sources"],
        "context_sources": row.get("context_sources", []),
        "warnings": row.get("warnings", []),
    })
    tu_items = [
        TURequirement.model_validate(item["requirement"])
        for item in (run.result or {}).get("checked_requirements", [])
    ]
    if not tu_items:
        raise HTTPException(409, "No TU requirements in saved report")

    model = CheckpointedDeepSeek(
        job_id, DeepSeekClient(), session_factory=session_factory,
    )
    agent = ZnPFirstAgent(
        ai_client=model, embedder=EmbeddingService(),
    )
    agent._requirements = {item.id: item for item in tu_items}
    try:
        model.check_control()
        retriever = RequirementRetriever(
            tu_items, agent.embedder,
            batch_size=agent.batch_size, neighbor_window=2,
        )
        page = retriever.search(atom)
        selected = page["candidates"]
        selected_by_id = {c["requirement_id"]: c for c in selected}
        context = retriever.context_details(
            [c["requirement_id"] for c in selected[:2]],
            known_ids=selected_by_id.keys(), include_section=False,
        )["items"] if selected else []
        for adjacent in context:
            rid = adjacent["requirement_id"]
            if rid not in selected_by_id:
                selected_by_id[rid] = {**adjacent, "similarity_score": 0.0}
        accepted = []
        rejected: Counter[str] = Counter()
        proposal_count = 0
        if selected:
            model.check_control()
            response = agent._judge(atom, selected, context)
            if not isinstance(response, dict) or not isinstance(response.get("links"), list):
                raise ValueError("DeepSeek returned an invalid matching payload")
            proposal_count = len(response["links"])
            for link in response["links"]:
                if not isinstance(link, dict):
                    rejected["invalid_shape"] += 1
                    continue
                rid = str(link.get("requirement_id") or "")
                candidate = selected_by_id.get(rid)
                if candidate is None:
                    rejected["unknown_requirement"] += 1
                    continue
                verified = agent._validated_link(
                    atom, agent._requirements[rid], link,
                    candidate["similarity_score"], rejected,
                )
                if verified is not None:
                    accepted.append({
                        "tu_requirement_id": rid,
                        "tu_clause_number": agent._requirements[rid].clause_number,
                        "tu_text": agent._requirements[rid].text,
                        "relation": verified["relation"],
                        "tu_quote": verified["tu_quote"],
                        "znp_quote": verified["quote"],
                        "reasoning": verified["reasoning"],
                        "confidence": verified["confidence"]
                            if verified["model_confidence_available"] else None,
                        "warnings": verified["warnings"],
                    })
        result = {
            "atom_id": atom_id,
            "clause_number": atom.clause_number,
            "model_checked": bool(selected),
            "candidate_count": len(selected),
            "proposed_links": proposal_count,
            "validated_links": accepted,
            "rejected_links": dict(rejected),
            "notice": "Это повторная проверка только связей одного атома. "
                      "Она не пересчитывает покрытие ТУ, аудит противоречий и "
                      "не меняет опубликованный итог сверки.",
        }
        model.check_control()
        review = ComparisonAtomReview(
            run_id=run.id, job_id=job_id, atom_id=atom_id, result=result,
        )
        db.add(review)
        db.commit()
        db.refresh(review)
        return {"review_id": review.id, "run_id": run_id, "atom_id": atom_id}
    finally:
        model.close()
