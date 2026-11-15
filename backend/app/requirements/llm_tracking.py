"""Per-comparison DeepSeek request accounting and bounded usage.

Never log or retain prompts, document text, credentials or raw model responses.
The cap limits *requests*, not billing cost (which also depends on token usage).
"""
from __future__ import annotations

import json
from collections import Counter
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
    ) -> None:
        self.model = model
        self.max_calls = max_calls
        self.phase_limits = {
            "atomization": atomization_limit,
            "targeted": targeted_limit,
        }
        self.attempted: Counter[str] = Counter()
        self.succeeded: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        self.rejected_by_limit: Counter[str] = Counter()

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
        if self.exhausted:
            self.rejected_by_limit[phase] += 1
            raise LLMCallLimitExceeded(phase, "global")
        if self.phase_exhausted(phase):
            self.rejected_by_limit[phase] += 1
            raise LLMCallLimitExceeded(phase, "phase")

        self.attempted[phase] += 1
        try:
            response = self.model.complete_json(
                system_prompt=system_prompt, user_prompt=user_prompt,
            )
        except Exception:
            self.errors[phase] += 1
            raise
        self.succeeded[phase] += 1
        return response

    def trace(self) -> dict:
        return {
            "model_calls_total": self.total,
            "model_calls_limit": self.max_calls,
            "attempted_by_phase": dict(self.attempted),
            "responses_by_phase": dict(self.succeeded),
            "errors_by_phase": dict(self.errors),
            "blocked_by_limit": dict(self.rejected_by_limit),
            "limit_reached": self.exhausted or bool(self.rejected_by_limit),
            "limits_by_phase": self.phase_limits,
        }
