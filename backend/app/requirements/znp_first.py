"""Conservative ZnP -> TU comparison pipeline.

Every ZnP atom is visited. Retrieval ranks TU candidates, while only
source-validated evidence can support an automatic positive verdict.
The legacy agent is retained for old API requests and regression comparison.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any, Callable
from uuid import uuid4

from app.ai.deepseek import DeepSeekClient, DeepSeekError
from app.ai.embeddings import EmbeddingService
from app.requirements.retrieval import RequirementRetriever
from app.requirements.schemas import (
    CheckRequirementsResponse, RequirementCheckItem, RequirementStatus,
    TURequirement, ZNPClause,
)
from app.requirements.tools import (
    tool_check_list_coverage, tool_search_znp_clauses, tool_validate_quotes,
    tool_verify_engineering_params,
)

MATCH_PROMPT = """Ты проверяешь одно положение ЗнП на соответствие требованиям ТУ.
Перед тобой положение ЗнП и предварительно найденные кандидаты ТУ.
Поиск уже выполнен Python; не делай вывод об отсутствии требования из одного списка кандидатов.
Найди все относящиеся к этому положению требования ТУ среди ПЕРЕДАННЫХ кандидатов.
Для каждого относящегося верни точные цитаты ИЗ ОБОИХ исходных текстов.
Не придумывай цитаты и идентификаторы. Не смешивай разные объекты, условные
разрешения, исключения, альтернативы, единицы измерения и направления неравенств.
Если данных недостаточно, используй partial; неверные числа или условия — conflicts.
Если нужен следующий пакет кандидатов, установи search_more=true.
Формат ответа — строго JSON:
{
  "links": [{
    "requirement_id": "id из списка кандидатов",
    "relation": "supports|conflicts|partial",
    "tu_quote": "точная подстрока ТУ",
    "znp_quote": "точная подстрока ЗнП, не более 600 символов",
    "confidence": 0.0,
    "reasoning": "краткое объяснение"
  }],
  "search_more": false
}
Если ни один кандидат не подходит: links=[].
Не объявляй соответствие при отсутствии проверяемых цитат.
"""


class ZnPFirstAgent:
    """End-to-end comparison using conservative ZnP statements as search queries."""

    def __init__(
        self,
        ai_client: DeepSeekClient | None = None,
        embedder: EmbeddingService | None = None,
        *,
        batch_size: int = 6,
        max_batches: int = 5,
    ) -> None:
        if batch_size < 1 or max_batches < 1:
            raise ValueError("Search limits must be positive")
        self.ai = ai_client or DeepSeekClient()
        self.embedder = embedder or EmbeddingService()
        self.batch_size = batch_size
        self.max_batches = max_batches

    def _judge(self, atom: ZNPClause, candidates: list[dict[str, Any]], context: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {
            "znp_atom": {
                "id": atom.id,
                "clause_number": atom.clause_number,
                "text": atom.text,
                "section_path": getattr(atom, "section_path", None),
                "context_sources": getattr(atom, "context_sources", []),
                "warnings": getattr(atom, "warnings", []),
            },
            "related_tu_context": context,
            "tu_candidates": [{
                "requirement_id": item["requirement_id"],
                "clause_number": item["clause_number"],
                "text": item["text"],
                "similarity_score": item["similarity_score"],
                "context_sources": item.get("context_sources", []),
            } for item in candidates],
        }
        return self.ai.complete_json(
            system_prompt=MATCH_PROMPT,
            user_prompt=json.dumps(payload, ensure_ascii=False),
        )

    @staticmethod
    def _validated_link(
        atom: ZNPClause, requirement: TURequirement,
        proposal: dict[str, Any], similarity: float,
    ) -> dict[str, Any] | None:
        relation = proposal.get("relation")
        if relation not in {"supports", "conflicts", "partial"}:
            return None
        tu_quote = proposal.get("tu_quote")
        znp_quote = proposal.get("znp_quote")
        if not isinstance(tu_quote, str) or not isinstance(znp_quote, str):
            return None
        quotes = tool_validate_quotes(requirement.text, tu_quote, atom.text, znp_quote)
        if not (quotes["tu_quote_valid"] and quotes["znp_quote_valid"]
                and quotes["znp_quote_atomic"]):
            return None
        # An exact citation must resolve to one actual source fragment, not just
        # to a reassembled clause spanning several pages.
        def source_for(value, clause):
            sources = getattr(clause, "sources", ()) or ()
            for source in sources:
                raw = source.get("text", "") if isinstance(source, dict) else source.text
                if " ".join(value.split()).casefold() in " ".join(raw.split()).casefold():
                    return source
            if sources:
                return None
            raw = getattr(clause, "fragment_text", None)
            if not raw:
                raw = clause.text
            return {"text": raw, "fragment_id": getattr(clause, "fragment_id", None),
                    "page": getattr(clause, "page", None)} if (
                " ".join(value.split()).casefold() in " ".join(raw.split()).casefold()
            ) else None
        tu_source = source_for(tu_quote, requirement)
        znp_source = source_for(znp_quote, atom)
        if tu_source is None or znp_source is None:
            return None
        def source_field(source, name):
            if isinstance(source, dict):
                return source.get(name)
            return getattr(source, name, None)
        try:
            confidence = max(0.0, min(1.0, float(proposal.get("confidence", 0))))
        except (ValueError, TypeError):
            confidence = 0.0
        params = tool_verify_engineering_params(requirement.text, atom.text)
        warnings = list(getattr(atom, "warnings", ()) or ())
        if relation == "supports":
            if not params["params_match"] or confidence < 0.7 or warnings:
                relation = "partial"
        if relation == "conflicts" and params["params_match"]:
            # Semantic opposition may be real, but Python cannot certify it.
            relation = "partial"
        return {
            "atom_id": atom.id,
            "clause_id": atom.id,
            "clause_number": atom.clause_number,
            "fragment_id": source_field(znp_source, "fragment_id"),
            "page": source_field(znp_source, "page"),
            "tu_fragment_id": source_field(tu_source, "fragment_id"),
            "tu_page": source_field(tu_source, "page"),
            "tu_quote": tu_quote,
            "quote": znp_quote,
            "relation": relation,
            "confidence": round(confidence, 3),
            "search_score": round(float(similarity), 3),
            "reasoning": str(proposal.get("reasoning") or ""),
            "param_check": params,
            "warnings": warnings,
        }

    def verify_all(
        self,
        tu_requirements: list[TURequirement],
        znp_clauses: list[ZNPClause],
        progress_callback: Callable[[str, int, int, str | None], None] | None = None,
    ) -> CheckRequirementsResponse:
        start = time.perf_counter()
        traces: list[dict[str, Any]] = []
        links: dict[str, list[dict[str, Any]]] = defaultdict(list)
        seen: set[tuple[str, str]] = set()
        all_requirements = {item.id: item for item in tu_requirements}
        znp_by_id = {item.id: item for item in znp_clauses}
        model_available = True
        model_error: str | None = None
        limited_atoms: list[str] = []
        if progress_callback:
            progress_callback("embedding", 0, len(znp_clauses), None)
        retriever = RequirementRetriever(
            tu_requirements, self.embedder, batch_size=self.batch_size,
            neighbor_window=2,
        )
        if progress_callback:
            progress_callback("verifying", 0, len(znp_clauses), None)

        for number, atom in enumerate(znp_clauses, start=1):
            atom_trace: dict[str, Any] = {
                "atom_id": atom.id,
                "clause_number": atom.clause_number,
                "steps": [],
            }
            # The mandatory first search never depends on an LLM action.
            page = retriever.search(atom)
            atom_trace["steps"].append({
                "tool": "search_tu", "candidates_count": len(page["candidates"]),
                "channels": page["ranking_channels"],
            })
            batches = 0
            while page["candidates"] and batches < self.max_batches:
                batches += 1
                candidates = page["candidates"]
                candidate_by_id = {item["requirement_id"]: item for item in candidates}
                if not model_available:
                    break
                if len(atom.text) > 10000 or any(len(c["text"]) > 10000 for c in candidates):
                    atom_trace["steps"].append({
                        "tool": "context_limit",
                        "reason": "An oversized source was not truncated for a compliance verdict",
                    })
                    break
                # Expand around the highest-ranked TU clauses while preserving
                # distinct IDs; neighboring requirements are evidence candidates too.
                context = retriever.context_details(
                    [candidate["requirement_id"] for candidate in candidates[:2]],
                    known_ids=candidate_by_id.keys(),
                    include_section=False,
                )["items"]
                for neighbor in context:
                    rid = neighbor["requirement_id"]
                    if rid not in candidate_by_id:
                        candidate_by_id[rid] = {
                            **neighbor, "similarity_score": 0.0,
                        }
                try:
                    reply = self._judge(atom, candidates, context)
                except DeepSeekError as exc:
                    model_error = str(exc)
                    model_available = False
                    atom_trace["steps"].append({"tool": "llm_fallback", "reason": model_error})
                    break
                if not isinstance(reply, dict) or not isinstance(reply.get("links"), list):
                    atom_trace["steps"].append({
                        "tool": "invalid_response", "reason": "Missing links array",
                    })
                    break
                added = 0
                for proposal in reply["links"]:
                    if not isinstance(proposal, dict):
                        continue
                    rid = str(proposal.get("requirement_id") or "")
                    candidate = candidate_by_id.get(rid)
                    if candidate is None or (atom.id, rid) in seen:
                        continue
                    valid = self._validated_link(
                        atom, all_requirements[rid], proposal,
                        candidate["similarity_score"],
                    )
                    if valid is not None:
                        links[rid].append(valid)
                        seen.add((atom.id, rid))
                        added += 1
                atom_trace["steps"].append({
                    "tool": "compare_candidates", "candidates": len(candidates),
                    "accepted_links": added,
                })
                if not page["has_more"] or (added and not reply.get("search_more", False)):
                    break
                page = retriever.search(atom, offset=batches * self.batch_size)
            if page["has_more"]:
                limited_atoms.append(atom.id)
            traces.append(atom_trace)
            if progress_callback:
                progress_callback("verifying", number, len(znp_clauses), atom.clause_number)

        checked: list[RequirementCheckItem] = []
        for requirement in tu_requirements:
            matches = links.get(requirement.id, [])
            supported = [link for link in matches if link["relation"] == "supports"]
            conflicts = [link for link in matches if link["relation"] == "conflicts"]
            partial = [link for link in matches if link["relation"] == "partial"]
            matched = supported[0] if supported else (conflicts[0] if conflicts else
                                                     (partial[0] if partial else None))
            evidence_text = "\n".join(link["quote"] for link in supported)
            coverage = tool_check_list_coverage(requirement.text, evidence_text)
            # Verify the entire set of documented parameters, not only each match.
            params = tool_verify_engineering_params(requirement.text, evidence_text)
            complete = bool(supported) and not conflicts and not partial
            complete = complete and coverage["covered"] and params["params_match"]
            status = RequirementStatus.fulfilled if complete else RequirementStatus.needs_review
            if conflicts:
                reason = f"Выявлено противоречий: {len(conflicts)}. Требуется проверка инженера."
            elif partial:
                reason = f"Есть {len(partial)} неоднозначных или частичных соответствий."
            elif complete:
                reason = f"Найдено проверенных подтверждений: {len(supported)}."
            else:
                reason = "Автоматическая проверка не установила достаточного подтверждения."
            targeted: list[dict[str, Any]] = []
            if not complete:
                # A targeted coverage check is advisory; never equate search failure with absence.
                targeted = tool_search_znp_clauses(requirement.text, znp_clauses, top_k=3)
                targeted = [{
                    "clause_id": item["clause_id"],
                    "clause_number": item["clause_number"],
                    "score": item["similarity_score"],
                } for item in targeted]
            details = {
                "version": 2,
                "pipeline": "znp_first_conservative",
                "matches": matches,
                "coverage_check": coverage,
                "param_check": params,
                "targeted_search": targeted,
                "znp_atoms_processed": len(znp_clauses),
                "candidate_limit_reached": bool(limited_atoms),
                "model_error": model_error,
                "unmatched": not bool(matches),
            }
            result_clause = znp_by_id[matched["clause_id"]] if matched else None
            checked.append(RequirementCheckItem(
                id=str(uuid4()),
                requirement=requirement,
                status=status,
                automatic_status=status,
                confidence_score=(min(link["confidence"] for link in supported) if complete else 0.0),
                matched_clause=result_clause,
                tu_quote=matched["tu_quote"] if matched else requirement.text[:120],
                znp_quote=matched["quote"] if matched else None,
                reasoning=reason,
                validation_details=details,
                is_confirmed_by_human=False,
                method="llm" if matches and model_available else "deterministic",
                channels=["lexical", "semantic"] if retriever.vectors is not None else ["lexical"],
            ))

        stats = {
            key: sum(item.status.value == key for item in checked)
            for key in ("fulfilled", "missing", "needs_review")
        }
        stats["total"] = len(checked)
        return CheckRequirementsResponse(
            checked_requirements=checked,
            stats=stats,
            trace_info={
                "pipeline": "znp_first_conservative",
                "execution_time_ms": round((time.perf_counter() - start) * 1000, 2),
                "znp_atoms_processed": len(znp_clauses),
                "znp_atoms_total": len(znp_clauses),
                "links_total": sum(len(entries) for entries in links.values()),
                "retrieval_limited_atoms": len(set(limited_atoms)),
                "agent_runs": traces,
                "embedding": self.embedder.status(),
                "degraded": not model_available or not self.embedder.enabled,
            },
        )
