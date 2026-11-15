"""Conservative ZnP -> TU comparison pipeline.

Every ZnP atom is visited. Retrieval ranks TU candidates, while only
source-validated evidence can support an automatic positive verdict.
The legacy agent is retained for old API requests and regression comparison.
"""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from typing import Any, Callable
from uuid import uuid4

from app.ai.deepseek import DeepSeekClient, DeepSeekError
from app.ai.embeddings import EmbeddingService
from app.requirements.retrieval import RequirementRetriever
from app.requirements.llm_tracking import LLMCallLimitExceeded
from app.requirements.coverage import (
    build_obligations, assess_coverage, all_obligations_covered,
    conflict_candidates, assess_conflicts, scope_overlap,
)
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
Важно: текст атома ЗнП может быть составлен из отдельных цитат и контекста,
соединённых переносом строки. Нельзя цитировать такую склейку целиком.
Поле znp_quote должно быть ОДНИМ непрерывным дословным фрагментом,
который присутствует одновременно в znp_atom.text и в одном из
znp_atom.source_fragments[].text. Аналогично tu_quote должен находиться
в одном исходном фрагменте ТУ. Не придумывай объединённые цитаты.
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

    def _judge(self, atom: ZNPClause, candidates: list[dict[str, Any]], context: list[dict[str, Any]], phase: str = "primary") -> dict[str, Any]:
        payload = {
            "phase": phase,
            "znp_atom": {
                "id": atom.id,
                "clause_number": atom.clause_number,
                "text": atom.text,
                "source_fragments": [
                    {"fragment_id": source.get("fragment_id"),
                     "text": str(source.get("text", ""))[:2400]}
                    for source in (atom.sources or [])[:3]
                    if isinstance(source, dict)
                ],
                "section_path": getattr(atom, "section_path", None),
                "context_sources": getattr(atom, "context_sources", []),
                "warnings": getattr(atom, "warnings", []),
            },
            "related_tu_context": context,
            "tu_candidates": [{
                "requirement_id": item["requirement_id"],
                "clause_number": item["clause_number"],
                "text": item["text"],
                "source_fragments": [
                    {"fragment_id": source.get("fragment_id"),
                     "text": str(source.get("text", ""))[:1600]}
                    for source in (
                        getattr(getattr(self, "_requirements", {}).get(item["requirement_id"]),
                                "sources", []) or []
                    )[:3] if isinstance(source, dict)
                ],
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
        rejection_counts: Counter[str] | None = None,
    ) -> dict[str, Any] | None:
        relation = proposal.get("relation")
        if relation not in {"supports", "conflicts", "partial"}:
            if rejection_counts is not None:
                rejection_counts["invalid_relation"] += 1
            return None
        tu_quote = proposal.get("tu_quote")
        znp_quote = proposal.get("znp_quote")
        if not isinstance(tu_quote, str) or not isinstance(znp_quote, str):
            if rejection_counts is not None:
                rejection_counts["missing_quotes"] += 1
            return None
        quotes = tool_validate_quotes(requirement.text, tu_quote, atom.text, znp_quote)
        if not (quotes["tu_quote_valid"] and quotes["znp_quote_valid"]
                and quotes["znp_quote_atomic"]):
            if rejection_counts is not None:
                if not quotes["tu_quote_valid"]:
                    rejection_counts["tu_quote_not_in_clause"] += 1
                elif not quotes["znp_quote_valid"]:
                    rejection_counts["znp_quote_not_in_atom"] += 1
                else:
                    rejection_counts["znp_quote_not_atomic"] += 1
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
            if rejection_counts is not None:
                rejection_counts[
                    "tu_quote_not_in_source" if tu_source is None
                    else "znp_quote_not_in_source"
                ] += 1
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
            if (not params["params_match"] or confidence < 0.7 or warnings
                    or not scope_overlap(requirement.text, atom.text)):
                relation = "partial"
        if relation == "conflicts" and params["params_match"]:
            # Semantic opposition may be real, but Python cannot certify it.
            relation = "partial"
        return {
            "link_id": f"{requirement.id}:{atom.id}",
            "atom_id": atom.id,
            "clause_id": atom.id,
            "atom_text": atom.text,
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
            "scope_consistent": scope_overlap(requirement.text, atom.text),
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
        evaluated: Counter[str] = Counter()
        rejected: Counter[str] = Counter()
        proposed_total = 0
        accepted_total = 0
        stopped_due_to_invalid_evidence = False
        all_requirements = {item.id: item for item in tu_requirements}
        self._requirements = all_requirements
        znp_by_id = {item.id: item for item in znp_clauses}
        model_available = True
        model_error: str | None = None
        limited_atoms: list[str] = []
        matching_limited = False
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
                "page": atom.page,
                "model_checked": False,
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
                if (hasattr(self.ai, "phase_exhausted")
                        and self.ai.phase_exhausted("matching")):
                    matching_limited = True
                    atom_trace["steps"].append({
                        "tool": "matching_budget", "reason": "Matching-phase request limit reached",
                    })
                    break
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
                    if isinstance(exc, LLMCallLimitExceeded) and exc.scope == "phase":
                        matching_limited = True
                        atom_trace["steps"].append({
                            "tool": "matching_budget", "reason": str(exc),
                        })
                        break
                    model_error = str(exc)
                    model_available = False
                    atom_trace["steps"].append({"tool": "llm_fallback", "reason": model_error})
                    break
                if not isinstance(reply, dict) or not isinstance(reply.get("links"), list):
                    atom_trace["steps"].append({
                        "tool": "invalid_response", "reason": "Missing links array",
                    })
                    break
                atom_trace["model_checked"] = True
                # A valid model response is still AI analysis when it concludes
                # that no match exists; it must not be counted as local fallback.
                for rid in candidate_by_id:
                    evaluated[rid] += 1
                added = 0
                proposed_total += len(reply["links"])
                for proposal in reply["links"]:
                    if not isinstance(proposal, dict):
                        rejected["invalid_link_shape"] += 1
                        continue
                    rid = str(proposal.get("requirement_id") or "")
                    candidate = candidate_by_id.get(rid)
                    if candidate is None:
                        rejected["unknown_candidate"] += 1
                        continue
                    if (atom.id, rid) in seen:
                        continue
                    valid = self._validated_link(
                        atom, all_requirements[rid], proposal,
                        candidate["similarity_score"], rejected,
                    )
                    if valid is not None:
                        links[rid].append(valid)
                        seen.add((atom.id, rid))
                        added += 1
                        accepted_total += 1
                atom_trace["steps"].append({
                    "tool": "compare_candidates", "candidates": len(candidates),
                    "accepted_links": added,
                })
                if sum(rejected.values()) >= 12 and accepted_total == 0:
                    stopped_due_to_invalid_evidence = True
                    model_available = False
                    model_error = (
                        "Остановлена платная проверка: модель вернула 12 или более "
                        "связей без проверяемых исходных цитат."
                    )
                    atom_trace["steps"].append({
                        "tool": "invalid_response", "reason": model_error,
                    })
                    break
                # Do not burn through later candidate pages unless the model
                # explicitly requests them. An empty links[] is a valid answer.
                if not page["has_more"] or not reply.get("search_more", False):
                    break
                page = retriever.search(atom, offset=batches * self.batch_size)
            if page["has_more"] and not matching_limited:
                limited_atoms.append(atom.id)
            traces.append(atom_trace)
            if progress_callback:
                progress_callback("verifying", number, len(znp_clauses), atom.clause_number)

        # Complete coverage is assessed per TU obligation, not from one merged
        # similarity score. We first look for gaps, then perform a targeted
        # second retrieval pass on ZnP; all resulting citations are revalidated.
        obligations = {item.id: build_obligations(item) for item in tu_requirements}
        coverage_results: dict[str, list[dict[str, Any]]] = {}
        targeted_results: dict[str, list[dict[str, Any]]] = {}
        targeted_calls = 0
        targeted_checked_atoms = set()
        targeted_limit_hit = False
        # Build the reverse-side semantic candidates once; if local embeddings
        # are unavailable, targeted retrieval falls back to lexical ranking.
        znp_vectors = (
            self.embedder.embed_documents([atom.text for atom in znp_clauses])
            if znp_clauses and self.embedder.enabled else []
        )
        if len(znp_vectors) != len(znp_clauses) or not self.embedder.enabled:
            znp_vectors = None

        for requirement in tu_requirements:
            entries = links[requirement.id]
            evidence = [x for x in entries if x["relation"] in {"supports", "partial"}
                        and x.get("scope_consistent", True)]
            initial = [
                assess_coverage(requirement, obligation, evidence, self.ai)
                for obligation in obligations[requirement.id]
            ] if model_available else []
            if getattr(self.ai, "exhausted", False):
                model_available = False
                model_error = "Достигнут лимит запросов DeepSeek для сверки"
            gap_queries = [
                obligation.text for obligation, result in
                zip(obligations[requirement.id], initial)
                if result["status"] != "fulfilled"
            ] if initial else [o.text for o in obligations[requirement.id]]
            targeted: list[dict[str, Any]] = []
            if model_available and gap_queries and not (
                hasattr(self.ai, "phase_exhausted")
                and self.ai.phase_exhausted("targeted")
            ):
                for query in list(dict.fromkeys(gap_queries))[:6]:
                    # Searching the opposite way here is a fallback exclusively
                    # for uncovered TU obligations, not the main architecture.
                    query_vector = (
                        self.embedder.embed_query(query) if znp_vectors is not None else None
                    )
                    candidates = tool_search_znp_clauses(
                        query, znp_clauses, top_k=min(len(znp_clauses), self.batch_size * 3),
                        query_embedding=query_vector,
                        clause_embeddings=znp_vectors if self.embedder.enabled else None,
                    )
                    for index in range(0, len(candidates), self.batch_size):
                        batch = candidates[index:index + self.batch_size]
                        for suggestion in batch:
                            atom = znp_by_id[suggestion["clause_id"]]
                            if (atom.id, requirement.id) in seen:
                                continue
                            target = {
                                "requirement_id": requirement.id,
                                "clause_number": requirement.clause_number,
                                "text": requirement.text,
                                "similarity_score": suggestion["similarity_score"],
                            }
                            targeted_calls += 1
                            try:
                                answer = self._judge(atom, [target], [], phase="targeted")
                            except DeepSeekError as exc:
                                if (isinstance(exc, LLMCallLimitExceeded)
                                        and exc.scope == "phase"):
                                    targeted_limit_hit = True
                                    break
                                model_error = str(exc)
                                model_available = False
                                break
                            if not isinstance(answer, dict) or not isinstance(answer.get("links"), list):
                                targeted.append({"atom_id": atom.id, "error": "invalid_response"})
                                continue
                            evaluated[requirement.id] += 1
                            targeted_checked_atoms.add(atom.id)
                            proposed_total += len(answer["links"])
                            for proposal in answer["links"]:
                                if not isinstance(proposal, dict) or str(proposal.get("requirement_id")) != requirement.id:
                                    continue
                                entry = self._validated_link(
                                    atom, requirement, proposal,
                                    suggestion["similarity_score"], rejected,
                                )
                                if entry is not None:
                                    entries.append(entry)
                                    seen.add((atom.id, requirement.id))
                                    accepted_total += 1
                                    targeted.append({"atom_id": atom.id, "result": entry["relation"]})
                                    break
                        if not model_available or targeted_limit_hit:
                            break
                    if not model_available or targeted_limit_hit:
                        break
            targeted_results[requirement.id] = targeted
            evidence = [x for x in entries if x["relation"] in {"supports", "partial"}
                        and x.get("scope_consistent", True)]
            # Never pay for the same coverage analysis twice when targeted
            # retrieval added no new evidence.
            if initial and not any(row.get("result") for row in targeted):
                coverage_results[requirement.id] = initial
            elif model_available:
                coverage_results[requirement.id] = [
                    assess_coverage(requirement, obligation, evidence, self.ai)
                    for obligation in obligations[requirement.id]
                ]
            else:
                coverage_results[requirement.id] = [{
                "obligation_id": obligation.id, "text": obligation.text,
                "status": "needs_review", "evidence_ids": [],
                "reasoning": "Модель недоступна, покрытие не подтверждено",
                "checked": False,
                } for obligation in obligations[requirement.id]]
            if getattr(self.ai, "exhausted", False):
                model_available = False
                model_error = "Достигнут лимит запросов DeepSeek для сверки"

        # Contradictions are sourced from the ENTIRE ZnP, independently of
        # the supporting-link retrieval. Only already-covered TU obligations
        # need an expensive audit; uncovered requirements cannot be approved.
        # Broad lexical overlap is not itself a contradiction. An incomplete
        # audit must never silently become a clean audit.
        audits: dict[str, dict[str, Any]] = {}
        for requirement in tu_requirements:
            covered = all_obligations_covered(coverage_results[requirement.id])
            candidates = (
                conflict_candidates(requirement, znp_clauses)
                if covered and links.get(requirement.id) else []
            )
            batch_limit = 10
            selected = candidates[:batch_limit]
            pending_count = len(candidates) - len(selected)
            confirmed: list[dict[str, Any]] = []
            uncertain: list[str] = []
            calls = 0
            if selected and model_available and not (
                hasattr(self.ai, "phase_exhausted")
                and self.ai.phase_exhausted("contradictions")
            ):
                confirmed, uncertain, calls = assess_conflicts(
                    requirement, selected, self.ai
                )
            elif selected:
                pending_count += len(selected)
            audits[requirement.id] = {
                "candidate_count": len(candidates),
                "candidates_checked": min(len(selected), calls * 8),
                "confirmed": confirmed,
                "uncertain_atom_ids": uncertain,
                "unreviewed_count": pending_count,
                "llm_calls": calls,
                "complete": not pending_count and not uncertain,
                "skipped_uncovered": not covered,
            }

        checked: list[RequirementCheckItem] = []
        for requirement in tu_requirements:
            matches = links.get(requirement.id, [])
            supported = [link for link in matches if link["relation"] == "supports"]
            partial = [link for link in matches if link["relation"] == "partial"]
            conflicts = [link for link in matches if link["relation"] == "conflicts"]
            audit = audits[requirement.id]
            conflicts.extend(audit["confirmed"])
            obligation_checks = coverage_results[requirement.id]
            matched = supported[0] if supported else (partial[0] if partial else
                                                     (matches[0] if matches else None))
            approved_evidence_ids = {
                evidence_id
                for row in obligation_checks if row["status"] == "fulfilled"
                for evidence_id in row["evidence_ids"]
            }
            evidence_text = "\n".join(
                znp_by_id[entry["atom_id"]].text
                for entry in matches if entry["link_id"] in approved_evidence_ids
            )
            params = tool_verify_engineering_params(requirement.text, evidence_text)
            coverage = tool_check_list_coverage(requirement.text, evidence_text)
            complete = (
                all_obligations_covered(obligation_checks)
                and bool(supported or partial)
                # Each obligation was checked against its OWN cited evidence.
                # Rechecking the concatenated text of different objects as one
                # parameter set incorrectly rejects valid compound requirements.
                and not conflicts
                and audit["complete"]
                and all(row.get("checked", False) for row in obligation_checks)
                and not any(entry.get("warnings") for entry in matches)
            )
            status = RequirementStatus.fulfilled if complete else RequirementStatus.needs_review
            if conflicts:
                reason = f"Обнаружены возможные противоречия ({len(conflicts)}); необходима проверка инженера."
            elif audit["uncertain_atom_ids"] or audit["unreviewed_count"]:
                remaining_audit = len(audit["uncertain_atom_ids"]) + audit["unreviewed_count"]
                reason = f"Контроль противоречий не завершён ({remaining_audit} кандидатов)."
            elif complete:
                reason = f"Все {len(obligation_checks)} обязательных условия ТУ подтверждены и проверены."
            else:
                remaining = sum(row["status"] != "fulfilled" for row in obligation_checks)
                reason = f"Не подтверждено условий ТУ: {remaining} из {len(obligation_checks)}."
            details = {
                "version": 3,
                "pipeline": "znp_first_conservative",
                "matches": matches + audit["confirmed"],
                "obligations": [row.as_dict() for row in obligations[requirement.id]],
                "obligation_coverage": obligation_checks,
                "coverage_check": coverage,
                "param_check": params,
                "targeted_search": targeted_results[requirement.id],
                "contradiction_audit": audit,
                "znp_atoms_processed": len(znp_clauses),
                "candidate_limit_reached": bool(limited_atoms),
                "model_error": model_error,
                "ai_model_evaluations": evaluated[requirement.id],
                "unmatched": not bool(matches),
            }
            result_clause = znp_by_id.get(matched["clause_id"]) if matched else None
            checked.append(RequirementCheckItem(
                id=str(uuid4()),
                requirement=requirement,
                status=status,
                automatic_status=status,
                confidence_score=(min(link["confidence"] for link in supported)
                                  if complete and supported else 0.0),
                matched_clause=result_clause,
                tu_quote=matched["tu_quote"] if matched else requirement.text[:120],
                znp_quote=matched["quote"] if matched else None,
                reasoning=reason,
                validation_details=details,
                is_confirmed_by_human=False,
                method="llm" if evaluated[requirement.id] else "deterministic",
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
                "tu_obligations_total": sum(len(items) for items in obligations.values()),
                "targeted_model_calls": targeted_calls,
                "independent_conflict_candidates": sum(a["candidate_count"] for a in audits.values()),
                "uncovered_obligations": sum(sum(r["status"] != "fulfilled" for r in rows)
                                            for rows in coverage_results.values()),
                "retrieval_limited_atoms": len(set(limited_atoms)),
                "agent_runs": traces,
                "znp_atom_results": [{
                    "atom_id": atom.id,
                    "clause_number": atom.clause_number,
                    "text": atom.text[:1800],
                    "page": atom.page,
                    "model_checked": (
                        trace["model_checked"] or atom.id in targeted_checked_atoms
                    ),
                    "candidates_count": (
                        trace["steps"][0].get("candidates_count", 0)
                        if trace["steps"] else 0
                    ),
                    "links": [{
                        "tu_requirement_id": requirement.id,
                        "tu_clause_number": requirement.clause_number,
                        "tu_text": requirement.text[:1000],
                        "relation": link["relation"],
                        "tu_quote": link["tu_quote"],
                        "znp_quote": link["quote"],
                        "reasoning": link.get("reasoning", ""),
                    } for requirement in tu_requirements
                    for link in links.get(requirement.id, [])
                    if link["atom_id"] == atom.id],
                } for atom, trace in zip(znp_clauses, traces)],
                "matching_limit_reached": matching_limited,
                "ai_evaluated_requirements": sum(bool(v) for v in evaluated.values()),
                "proposed_links": proposed_total,
                "validated_links": accepted_total,
                "rejected_links": dict(rejected),
                "stopped_due_to_invalid_evidence": stopped_due_to_invalid_evidence,
                "targeted_limit_reached": targeted_limit_hit,
                "embedding": self.embedder.status(),
                "degraded": not model_available or targeted_limit_hit or matching_limited,
            },
        )
