"""Агентный цикл проверки соответствия ТУ и ЗнП с трассировкой шагов."""
import json
import time
from typing import Any
from uuid import uuid4

from app.ai.deepseek import DeepSeekClient, DeepSeekError
from app.requirements.schemas import (
    CheckRequirementsResponse,
    LLMVerificationDecision,
    RequirementCheckItem,
    RequirementStatus,
    TURequirement,
    ZNPClause,
)
from app.requirements.tools import (
    tool_get_neighbor_clauses,
    tool_search_znp_clauses,
    tool_validate_quotes,
    tool_verify_engineering_params,
)

AGENT_SYSTEM_PROMPT = """Ты ИИ-агент инженерной экспертизы по технологическому присоединению.
Твоя задача — проверить, отражено ли требование из Технических условий (ТУ) в Задании на проектирование (ЗнП).

На каждом шаге ты сам выбираешь инструмент или завершаешь проверку.
Доступные инструменты:
1. "search_znp": {"query": "строка поиска"} — поиск подходящих пунктов в ЗнП.
2. "get_neighbors": {"clause_id": "id пункта"} — чтение соседних пунктов ЗнП для уточнения контекста.
3. "verify_params": {"znp_clause_id": "id пункта"} — сверка числовых параметров (мощность, напряжение, категория, точность).
4. "validate_quotes": {"tu_quote": "точная цитата из ТУ", "znp_clause_id": "id пункта", "znp_quote": "точная цитата из ЗнП"} — проверка подлинности цитат.

Формат ответа — СТРОГО JSON одного из двух видов:
Вариант А (вызов инструмента):
{
  "action": "call_tool",
  "tool_name": "search_znp" | "get_neighbors" | "verify_params" | "validate_quotes",
  "tool_args": {...},
  "thought": "почему выбран этот шаг"
}

Вариант Б (завершение проверки после получения данных от инструментов):
{
  "action": "finish",
  "status": "fulfilled" | "missing" | "needs_review",
  "confidence_score": 0.0..1.0,
  "matched_znp_clause_id": "id пункта или null",
  "tu_quote": "точная подстрока из требования ТУ",
  "znp_quote": "точная подстрока из пункта ЗнП или null",
  "reasoning": "объяснение для инженера"
}
Правило эскалации: если параметры расходятся, формулировка неоднозначна или уверенность ниже 0.70 — ставь "needs_review".
"""


class RequirementsAgent:
    """Агент сверки требований ТУ и ЗнП с журналом трассировки."""

    def __init__(self, ai_client: DeepSeekClient | None = None, max_steps: int = 4):
        self.ai = ai_client or DeepSeekClient()
        self.max_steps = max_steps

    def verify_all(
        self,
        tu_requirements: list[TURequirement],
        znp_clauses: list[ZNPClause],
    ) -> CheckRequirementsResponse:
        started_at = time.perf_counter()
        checked_items: list[RequirementCheckItem] = []
        all_traces: list[dict[str, Any]] = []

        znp_by_id = {c.id: c for c in znp_clauses}

        for req in tu_requirements:
            decision, req_trace = self._verify_single_requirement(req, znp_clauses, znp_by_id)
            all_traces.append({
                "requirement_id": req.id,
                "clause_number": req.clause_number,
                "steps": req_trace,
            })

            matched_clause = (
                znp_by_id.get(decision.matched_znp_clause_id)
                if decision.matched_znp_clause_id
                else None
            )

            checked_items.append(
                RequirementCheckItem(
                    id=str(uuid4()),
                    requirement=req,
                    status=decision.status,
                    confidence_score=decision.similarity_score,
                    matched_clause=matched_clause,
                    tu_quote=decision.tu_quote,
                    znp_quote=decision.znp_quote,
                    reasoning=decision.reasoning,
                    is_confirmed_by_human=False,
                )
            )

        stats = {
            "fulfilled": sum(1 for i in checked_items if i.status == RequirementStatus.fulfilled),
            "missing": sum(1 for i in checked_items if i.status == RequirementStatus.missing),
            "needs_review": sum(1 for i in checked_items if i.status == RequirementStatus.needs_review),
            "total": len(checked_items),
        }
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)

        return CheckRequirementsResponse(
            checked_requirements=checked_items,
            stats=stats,
            trace_info={
                "execution_time_ms": elapsed_ms,
                "agent_runs": all_traces,
            },
        )

    def _verify_single_requirement(
        self,
        req: TURequirement,
        znp_clauses: list[ZNPClause],
        znp_by_id: dict[str, ZNPClause],
    ) -> tuple[LLMVerificationDecision, list[dict[str, Any]]]:
        trace: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []

        for step_num in range(1, self.max_steps + 1):
            user_context = {
                "step": step_num,
                "max_steps": self.max_steps,
                "tu_requirement": {
                    "id": req.id,
                    "clause_number": req.clause_number,
                    "category": req.category.value,
                    "text": req.text,
                },
                "history": observations,
            }

            step_start = time.perf_counter()
            try:
                llm_reply = self.ai.complete_json(
                    system_prompt=AGENT_SYSTEM_PROMPT,
                    user_prompt=json.dumps(user_context, ensure_ascii=False),
                )
            except DeepSeekError as exc:
                # Если LLM недоступна или ключ не сконфигурирован — выполняем инструментальный цикл локально
                trace.append({
                    "step": step_num,
                    "tool": "llm_fallback",
                    "reason": str(exc),
                })
                return self._deterministic_agent_fallback(req, znp_clauses, trace)

            llm_duration = round((time.perf_counter() - step_start) * 1000, 2)
            action = llm_reply.get("action")

            if action == "call_tool" and step_num < self.max_steps:
                tool_name = str(llm_reply.get("tool_name", "search_znp"))
                tool_args = llm_reply.get("tool_args") or {}
                tool_out = self._execute_tool(tool_name, tool_args, req, znp_clauses, znp_by_id)

                step_record = {
                    "step": step_num,
                    "thought": llm_reply.get("thought", ""),
                    "tool": tool_name,
                    "args": tool_args,
                    "output": tool_out,
                    "duration_ms": llm_duration,
                }
                trace.append(step_record)
                observations.append({"tool": tool_name, "result": tool_out})
                continue

            if action == "finish":
                return self._finalize_llm_decision(llm_reply, req, znp_clauses, znp_by_id, trace, llm_duration)

            if step_num == self.max_steps:
                return self._deterministic_agent_fallback(req, znp_clauses, trace)

        return self._deterministic_agent_fallback(req, znp_clauses, trace)

    def _execute_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        req: TURequirement,
        znp_clauses: list[ZNPClause],
        znp_by_id: dict[str, ZNPClause],
    ) -> Any:
        if tool_name == "search_znp":
            query = str(tool_args.get("query") or req.text)
            return tool_search_znp_clauses(query, znp_clauses, top_k=3)

        if tool_name == "get_neighbors":
            cid = str(tool_args.get("clause_id") or "")
            return tool_get_neighbor_clauses(cid, znp_clauses, window=1)

        if tool_name == "verify_params":
            cid = str(tool_args.get("znp_clause_id") or "")
            clause = znp_by_id.get(cid)
            return tool_verify_engineering_params(req.text, clause.text if clause else "")

        if tool_name == "validate_quotes":
            cid = str(tool_args.get("znp_clause_id") or "")
            clause = znp_by_id.get(cid)
            return tool_validate_quotes(
                tu_full_text=req.text,
                tu_quote=tool_args.get("tu_quote"),
                znp_full_text=clause.text if clause else None,
                znp_quote=tool_args.get("znp_quote"),
            )

        return {"error": f"Unknown tool: {tool_name}"}

    def _finalize_llm_decision(
        self,
        llm_reply: dict[str, Any],
        req: TURequirement,
        znp_clauses: list[ZNPClause],
        znp_by_id: dict[str, ZNPClause],
        trace: list[dict[str, Any]],
        llm_duration: float,
    ) -> tuple[LLMVerificationDecision, list[dict[str, Any]]]:
        raw_matched_id = llm_reply.get("matched_znp_clause_id")
        matched_id = str(raw_matched_id).strip() if raw_matched_id else None

        matched_clause = znp_by_id.get(matched_id) if matched_id else None
        if not matched_clause and matched_id:
            matched_clause = next(
                (c for c in znp_clauses if c.clause_number.strip() == matched_id),
                None,
            )

        raw_tu_quote = llm_reply.get("tu_quote")
        tu_quote = str(raw_tu_quote).strip() if isinstance(raw_tu_quote, str) and raw_tu_quote.strip() else req.text[:120]

        raw_znp_quote = llm_reply.get("znp_quote")
        if matched_clause:
            znp_quote = (
                str(raw_znp_quote).strip()
                if isinstance(raw_znp_quote, str) and raw_znp_quote.strip()
                else matched_clause.text[:120]
            )
        else:
            znp_quote = None

        quote_check = tool_validate_quotes(
            req.text,
            tu_quote,
            matched_clause.text if matched_clause else None,
            znp_quote,
        )
        if not quote_check["tu_quote_valid"]:
            tu_quote = req.text[:120]
        if matched_clause and not quote_check["znp_quote_valid"]:
            znp_quote = matched_clause.text[:120]

        param_check = tool_verify_engineering_params(
            req.text,
            matched_clause.text if matched_clause else "",
        )

        raw_status = llm_reply.get("status") or "needs_review"
        try:
            status = RequirementStatus(str(raw_status).strip())
        except ValueError:
            status = RequirementStatus.needs_review

        raw_conf = llm_reply.get("confidence_score")
        try:
            confidence = float(raw_conf) if raw_conf is not None else 0.7
            if 1.0 < confidence <= 100.0:
                confidence /= 100.0
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.7

        reasoning = str(llm_reply.get("reasoning") or "Проверка выполнена ИИ-агентом.")

        if status == RequirementStatus.fulfilled and matched_clause is None:
            status = RequirementStatus.needs_review
            reasoning += " [Не указан подтверждающий пункт ЗнП]"
            confidence = min(confidence, 0.60)

        if not param_check["params_match"] and status == RequirementStatus.fulfilled:
            status = RequirementStatus.needs_review
            reasoning += f" [Валидатор параметров: {'; '.join(param_check['mismatches'])}]"
            confidence = min(confidence, 0.65)

        if confidence < 0.70 and status == RequirementStatus.fulfilled:
            status = RequirementStatus.needs_review

        trace.append({
            "step": len(trace) + 1,
            "tool": "finish_and_validate",
            "quote_check": quote_check,
            "param_check": param_check,
            "final_status": status.value,
            "duration_ms": llm_duration,
        })

        return (
            LLMVerificationDecision(
                status=status,
                similarity_score=round(confidence, 3),
                matched_znp_clause_id=matched_clause.id if matched_clause else None,
                tu_quote=tu_quote,
                znp_quote=znp_quote,
                reasoning=reasoning,
            ),
            trace,
        )

    def _deterministic_agent_fallback(
        self,
        req: TURequirement,
        znp_clauses: list[ZNPClause],
        trace: list[dict[str, Any]],
    ) -> tuple[LLMVerificationDecision, list[dict[str, Any]]]:
        """Полноценный инструментальный прогон (4 инструмента) при работе без внешнего API."""
        # 1. Поиск кандидатов
        candidates = tool_search_znp_clauses(req.text, znp_clauses, top_k=3)
        trace.append({"step": len(trace) + 1, "tool": "search_znp", "candidates_count": len(candidates)})

        if not candidates or candidates[0]["similarity_score"] < 0.15:
            return (
                LLMVerificationDecision(
                    status=RequirementStatus.missing,
                    similarity_score=0.85,
                    matched_znp_clause_id=None,
                    tu_quote=req.text,
                    znp_quote=None,
                    reasoning="В тексте ЗнП не найдено пунктов, соответствующих данному требованию ТУ.",
                ),
                trace,
            )

        best = candidates[0]
        # 2. Запрос контекста соседей, если совпадение пограничное
        if best["similarity_score"] < 0.45:
            neighbors = tool_get_neighbor_clauses(best["clause_id"], znp_clauses, window=1)
            trace.append({"step": len(trace) + 1, "tool": "get_neighbors", "neighbors_loaded": len(neighbors)})

        # 3. Валидация инженерных параметров
        param_res = tool_verify_engineering_params(req.text, best["text"])
        trace.append({"step": len(trace) + 1, "tool": "verify_params", "output": param_res})

        # 4. Валидация цитат
        quote_res = tool_validate_quotes(req.text, req.text, best["text"], best["text"])
        trace.append({"step": len(trace) + 1, "tool": "validate_quotes", "output": quote_res})

        if not param_res["params_match"]:
            return (
                LLMVerificationDecision(
                    status=RequirementStatus.needs_review,
                    similarity_score=0.65,
                    matched_znp_clause_id=best["clause_id"],
                    tu_quote=req.text,
                    znp_quote=best["text"],
                    reasoning="Найдено расхождение параметров: " + "; ".join(param_res["mismatches"]),
                ),
                trace,
            )

        if best["similarity_score"] >= 0.35:
            return (
                LLMVerificationDecision(
                    status=RequirementStatus.fulfilled,
                    similarity_score=max(0.75, best["similarity_score"]),
                    matched_znp_clause_id=best["clause_id"],
                    tu_quote=req.text,
                    znp_quote=best["text"],
                    reasoning=f"Требование отражено в пункте {best['clause_number']}, параметры совпадают.",
                ),
                trace,
            )

        return (
            LLMVerificationDecision(
                status=RequirementStatus.needs_review,
                similarity_score=0.55,
                matched_znp_clause_id=best["clause_id"],
                tu_quote=req.text,
                znp_quote=best["text"],
                reasoning="Частичное семантическое совпадение, требуется подтверждение инженером.",
            ),
            trace,
        )