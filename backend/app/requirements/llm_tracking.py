"""Per-comparison DeepSeek request accounting and bounded usage.

Never log or retain prompts, document text, credentials or raw model responses.
The cap limits *requests*, not billing cost (which also depends on token usage).
"""
from __future__ import annotations

import json
import time
from collections import Counter

from app.ai import diagnostics
from typing import Any

from app.ai.deepseek import DeepSeekError


class LLMCallLimitExceeded(DeepSeekError):
    def __init__(self, phase: str, scope: str):
        self.phase = phase
        self.scope = scope
        super().__init__(f"DeepSeek {scope} request limit reached during {phase}")


class TrackedRequirementsLLM:
    def __init__(
        self,
        model: Any,
        *,
        max_calls: int = 250,
        atomization_limit: int = 40,
        targeted_limit: int = 35,
        matching_limit: int = 120,
        coverage_limit: int = 65,
        contradictions_limit: int = 15,
    ) -> None:
        self.model = model
        if hasattr(model, "bind_ledger"):
            model.bind_ledger(self)
        self.replayed: Counter[str] = Counter()
        self.max_calls = max_calls
        self.phase_limits = {
            "atomization": atomization_limit,
            "targeted": targeted_limit,
            "matching": matching_limit,
            "coverage": coverage_limit,
            "contradictions": contradictions_limit,
        }
        self.attempted: Counter[str] = Counter()
        self.succeeded: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        self.rejected_by_limit: Counter[str] = Counter()
        self.elapsed_ms: Counter[str] = Counter()

    def configure_full_run_limits(
        self, *, atom_count: int, obligation_count: int,
        requirement_count: int, matching_batches: int,
    ) -> None:
        # Matching visits each atom up to max_batches times; coverage may run
        # once before and once after targeted search. The audit checks at most
        # ten candidates per TU in batches of eight. The global cap still wins.
        self.phase_limits["matching"] = min(
            self.max_calls, max(1, atom_count * matching_batches),
        )
        self.phase_limits["coverage"] = min(
            self.max_calls, max(1, obligation_count * 2),
        )
        self.phase_limits["contradictions"] = min(
            self.max_calls, max(1, requirement_count * 2),
        )
        diagnostics.record("ledger.full_run_limits", phase_caps=self.phase_limits)

    @staticmethod
    def phase(user_prompt: str) -> str:
        try:
            payload = json.loads(user_prompt)
        except (TypeError, ValueError):
            return "other"
        if not isinstance(payload, dict):
            return "other"
        if "clause_text" in payload:
            return "atomization"
        if "obligation" in payload:
            return "coverage"
        if payload.get("phase") == "targeted" and "tu_requirement" in payload and "znp_candidates" in payload:
            return "targeted"
        if "znp_atom" in payload:
            return "targeted" if payload.get("phase") == "targeted" else "matching"
        if "candidates" in payload and "requirement" in payload:
            return "contradictions"
        return "other"

    @property
    def total(self) -> int:
        return sum(self.attempted.values())

    @property
    def exhausted(self) -> bool:
        return self.total >= self.max_calls

    def phase_exhausted(self, phase: str) -> bool:
        cap = self.phase_limits.get(phase)
        return self.exhausted or (cap is not None and self.attempted[phase] >= cap)

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> dict:
        phase = self.phase(user_prompt)
        subject = ""
        candidate_count = None
        if diagnostics.enabled():
            try:
                payload = json.loads(user_prompt)
                if isinstance(payload, dict):
                    item = payload.get("znp_atom") or payload.get("tu_requirement")
                    if isinstance(item, dict):
                        subject = str(item.get("id") or "")
                    if "znp_candidates" in payload:
                        candidate_count = len(payload["znp_candidates"])
                    elif "tu_candidates" in payload:
                        candidate_count = len(payload["tu_candidates"])
            except (ValueError, TypeError):
                pass
        with diagnostics.phase_scope(phase, subject):
            if hasattr(self.model, "checkpoint_lookup"):
                saved = self.model.checkpoint_lookup(
                    system_prompt=system_prompt, user_prompt=user_prompt,
                )
                if saved is not None:
                    self.replayed[phase] += 1
                    return saved
            if self.exhausted:
                self.rejected_by_limit[phase] += 1
                diagnostics.record(
                    "ledger.limit", scope="global", used=self.total,
                    limit=self.max_calls, attempted_phase=self.attempted[phase],
                )
                raise LLMCallLimitExceeded(phase, "global")
            if self.phase_exhausted(phase):
                self.rejected_by_limit[phase] += 1
                diagnostics.record(
                    "ledger.limit", scope="phase", used=self.attempted[phase],
                    limit=self.phase_limits[phase], attempted_total=self.total,
                )
                raise LLMCallLimitExceeded(phase, "phase")

            self.attempted[phase] += 1
            started = time.monotonic()
            diagnostics.record(
                "ledger.request", phase_index=self.attempted[phase],
                total_index=self.total, candidate_count=candidate_count,
                prompt_chars=len(user_prompt), phase_cap=self.phase_limits.get(phase),
                total_cap=self.max_calls,
            )
            try:
                response = self.model.complete_json(
                    system_prompt=system_prompt, user_prompt=user_prompt,
                )
            except Exception as exc:
                self.errors[phase] += 1
                diagnostics.record(
                    "ledger.failure", error_type=type(exc).__name__,
                    truncated="truncated" in str(exc).casefold(),
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
                raise
            finally:
                self.elapsed_ms[phase] += (time.monotonic() - started)*1000
            self.succeeded[phase] += 1
            diagnostics.record(
                "ledger.success", response_keys=sorted(response) if isinstance(response, dict) else [],
                elapsed_ms=round((time.monotonic() - started) * 1000),
                links_count=len(response["links"]) if isinstance(response, dict)
                    and isinstance(response.get("links"), list) else None,
            )
            return response

    def trace(self) -> dict:
        return {
            "model_calls_total": self.total,
            "model_calls_limit": self.max_calls,
            "attempted_by_phase": dict(self.attempted),
            "responses_by_phase": dict(self.succeeded),
            "replayed_by_phase": dict(self.replayed),
            "errors_by_phase": dict(self.errors),
            "blocked_by_limit": dict(self.rejected_by_limit),
            "limit_reached": self.exhausted or bool(self.rejected_by_limit),
            "limits_by_phase": self.phase_limits,
            "elapsed_ms_by_phase": {k: round(v, 3) for k, v in self.elapsed_ms.items()},
        }
