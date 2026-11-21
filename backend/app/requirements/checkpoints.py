"""Durable per-request LLM checkpoints, safe replay and cooperative pause.

The database records a request as SENT *before* it is handed to DeepSeek,
and commits a validated JSON object *before* returning it to the pipeline.
If the process dies in between, the billing outcome is unknowable. Resuming
that request requires explicit acknowledgement; we never silently repay it.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from sqlalchemy import func, select

from app.ai import diagnostics
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.models import ComparisonLLMCheckpoint, OperationJob

CHECKPOINT_SCHEMA = "comparison-llm-v1"


class ComparisonPause(BaseException):
    """Stop without allowing blanket except Exception to swallow this signal."""


class ComparisonUncertain(ComparisonPause):
    """Sent request has no durable answer; re-sending can incur another charge."""


class ComparisonInterrupted(ComparisonPause):
    """Model failed, but earlier successful calls remain safely checkpointed."""


def request_fingerprint(system_prompt: str, user_prompt: str) -> str:
    payload = json.dumps({
        "schema": CHECKPOINT_SCHEMA,
        "model": settings.deepseek_model,
        "thinking": settings.deepseek_thinking,
        "max_tokens": settings.deepseek_max_tokens,
        "system": system_prompt,
        "user": user_prompt,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def checkpoint_counts(job_id: str) -> dict:
    with SessionLocal() as db:
        rows = db.scalars(select(ComparisonLLMCheckpoint).where(
            ComparisonLLMCheckpoint.job_id == job_id,
        )).all()
        return {
            "saved": sum(row.status == "succeeded" for row in rows),
            "uncertain": sum(row.status in ("sent", "failed") for row in rows),
            "requests_attempted": sum(row.attempts for row in rows),
            "by_phase": {
                name: sum(row.status == "succeeded" and row.phase == name for row in rows)
                for name in {row.phase for row in rows}
            },
        }


def _control(db, job_id: str) -> OperationJob:
    job = db.get(OperationJob, job_id)
    if job is None:
        raise ComparisonPause("Operation no longer exists")
    db.refresh(job)
    if job.pause_requested or job.status == "paused":
        raise ComparisonPause("Операция поставлена на паузу")
    if job.cancel_requested or job.status == "cancelled":
        raise ComparisonPause("Операция остановлена")
    if job.status not in ("running", "queued"):
        raise ComparisonPause("Operation is not active")
    return job


class CheckpointedDeepSeek:
    """Wrap an existing model and persist its successful complete_json calls.

    Only the same job can reuse responses. Across reruns no cached answer is
    silently reused. The exact prompts and model configuration form the key.
    """

    def __init__(self, job_id: str, model):
        self.job_id = job_id
        self.model = model
        self.ledger = None

    def bind_ledger(self, ledger) -> None:
        self.ledger = ledger

    def check_control(self) -> None:
        with SessionLocal() as db:
            _control(db, self.job_id)

    def _identity(self, system_prompt: str, user_prompt: str):
        fingerprint = request_fingerprint(system_prompt, user_prompt)
        from app.requirements.llm_tracking import TrackedRequirementsLLM
        phase = TrackedRequirementsLLM.phase(user_prompt)
        return fingerprint, phase

    def checkpoint_lookup(self, *, system_prompt: str, user_prompt: str):
        fingerprint, phase = self._identity(system_prompt, user_prompt)
        with SessionLocal() as db:
            job = _control(db, self.job_id)
            row = db.scalar(select(ComparisonLLMCheckpoint).where(
                ComparisonLLMCheckpoint.job_id == self.job_id,
                ComparisonLLMCheckpoint.fingerprint == fingerprint,
            ))
            if row is None:
                return None
            if row.status == "succeeded":
                diagnostics.record(
                    "checkpoint.replay", fingerprint=fingerprint[:16],
                    phase=phase, calls_saved=row.attempts,
                )
                return deepcopy(row.response)
            if not bool(job.params.get("retry_uncertain", False)):
                raise ComparisonUncertain(
                    "Ранее отправленный запрос не имеет сохранённого ответа. "
                    "Повтор может оплачиваться. Возобновите с подтверждением риска."
                )
            return None

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> dict:
        cached = self.checkpoint_lookup(
            system_prompt=system_prompt, user_prompt=user_prompt,
        )
        if cached is not None:
            return cached
        fingerprint, phase = self._identity(system_prompt, user_prompt)
        with SessionLocal() as db:
            job = _control(db, self.job_id)
            rows = db.scalars(select(ComparisonLLMCheckpoint).where(
                ComparisonLLMCheckpoint.job_id == self.job_id,
            )).all()
            attempted = sum(row.attempts for row in rows)
            if attempted >= settings.requirements_llm_max_calls:
                raise ComparisonInterrupted(
                    f"Общий лимит платных запросов исчерпан ({attempted}). "
                    "Увеличьте его осознанно и возобновите работу."
                )
            if self.ledger is not None:
                phase_count = sum(row.attempts for row in rows if row.phase == phase)
                cap = self.ledger.phase_limits.get(phase)
                if cap is not None and phase_count >= cap:
                    raise ComparisonInterrupted(
                        f"Лимит этапа {phase} исчерпан ({phase_count}/{cap})"
                    )
            row = next((row for row in rows if row.fingerprint == fingerprint), None)
            if row is None:
                row = ComparisonLLMCheckpoint(
                    job_id=self.job_id, fingerprint=fingerprint, phase=phase,
                    status="sent", attempts=0,
                )
                db.add(row)
            elif not bool(job.params.get("retry_uncertain", False)):
                raise ComparisonUncertain("Неизвестен результат ранее отправленного вызова")
            row.status = "sent"
            row.response = None
            row.last_error = None
            row.attempts += 1
            db.commit()
        diagnostics.record(
            "checkpoint.sent", fingerprint=fingerprint[:16], phase=phase,
        )
        try:
            result = self.model.complete_json(
                system_prompt=system_prompt, user_prompt=user_prompt,
            )
            if not isinstance(result, dict):
                raise ValueError("DeepSeek must return a JSON object")
        except Exception as exc:
            with SessionLocal() as db:
                row = db.scalar(select(ComparisonLLMCheckpoint).where(
                    ComparisonLLMCheckpoint.job_id == self.job_id,
                    ComparisonLLMCheckpoint.fingerprint == fingerprint,
                ))
                if row is not None:
                    row.status = "failed"
                    row.last_error = f"{type(exc).__name__}: {str(exc)[:250]}"
                    db.commit()
            diagnostics.record(
                "checkpoint.failed", fingerprint=fingerprint[:16],
                phase=phase, error_type=type(exc).__name__,
            )
            raise ComparisonInterrupted(
                f"DeepSeek: {type(exc).__name__}. Ранее сохранённые ответы не потеряны. "
                "Повтор запроса требует подтверждения из-за возможного списания."
            ) from exc

        with SessionLocal() as db:
            row = db.scalar(select(ComparisonLLMCheckpoint).where(
                ComparisonLLMCheckpoint.job_id == self.job_id,
                ComparisonLLMCheckpoint.fingerprint == fingerprint,
            ))
            if row is None:
                raise ComparisonInterrupted("Checkpoint disappeared before save")
            row.status = "succeeded"
            row.response = deepcopy(result)
            row.last_error = None
            db.commit()

        diagnostics.record(
            "checkpoint.saved", fingerprint=fingerprint[:16], phase=phase,
        )
        self.check_control()
        return result

    def close(self) -> None:
        if hasattr(self.model, "close"):
            self.model.close()
