"""Durable DeepSeek checkpoints: replay, crash ambiguity, pause and restart.

All tests use a fake in-process model and SQLite. No paid API calls.
"""
from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import pytest

from app.core.db import Base
from app.core.models import (
    ComparisonLLMCheckpoint, OperationJob, Project,
)
from app.core.config import settings
from app.requirements.checkpoints import (
    CheckpointedDeepSeek, ComparisonInterrupted, ComparisonPause,
    ComparisonUncertain, checkpoint_counts, request_fingerprint,
)
from app import operations


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Factory() as db:
        project = Project(name="checkpoint test")
        db.add(project)
        db.flush()
        job = OperationJob(
            project_id=project.id, kind="comparison", status="running",
            stage="verifying", params={"pair_ids":[]},
            message="working",
        )
        db.add(job)
        db.commit()
        key = job.id
    yield Factory, key
    engine.dispose()


class Spy:
    def __init__(self):
        self.calls = 0

    def complete_json(self, **kwargs):
        self.calls += 1
        return {"links": [], "search_more": False}


def test_saved_response_replayed_after_worker_reconstruction_without_new_charge(database):
    Factory, job_id = database
    fake = Spy()
    first = CheckpointedDeepSeek(job_id, fake, Factory)
    payload = '{"znp_atom":{"id":"a1"},"tu_candidates":[]}'
    response = first.complete_json(system_prompt="compare", user_prompt=payload)
    assert response == {"links": [], "search_more": False}

    after_restart = CheckpointedDeepSeek(job_id, fake, Factory)
    assert after_restart.complete_json(
        system_prompt="compare", user_prompt=payload,
    ) == response
    assert fake.calls == 1
    assert checkpoint_counts(job_id, Factory)["saved"] == 1


def test_unknown_sent_request_requires_explicit_replay_consent(database):
    Factory, job_id = database

    class Crash:
        def __init__(self):
            self.calls = 0

        def complete_json(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt("simulated hard process exit")
            return {"links":[]}

    provider = Crash()
    request = '{"znp_atom":{"id":"a3"},"tu_candidates":[]}'
    with pytest.raises(KeyboardInterrupt):
        CheckpointedDeepSeek(job_id, provider, Factory).complete_json(
            system_prompt="compare", user_prompt=request,
        )
    with Factory() as db:
        row = db.scalar(select(ComparisonLLMCheckpoint))
        assert row.status == "sent"
    with pytest.raises(ComparisonUncertain):
        CheckpointedDeepSeek(job_id, provider, Factory).complete_json(
            system_prompt="compare", user_prompt=request,
        )
    assert provider.calls == 1
    with Factory() as db:
        job = db.get(OperationJob, job_id)
        job.params = {**job.params, "retry_uncertain": True}
        db.commit()
    assert CheckpointedDeepSeek(job_id, provider, Factory).complete_json(
        system_prompt="compare", user_prompt=request,
    ) == {"links":[]}
    assert provider.calls == 2


def test_pause_blocks_even_a_cached_request(database):
    Factory, job_id = database
    fake = Spy()
    req = '{"znp_atom":{"id":"a4"},"tu_candidates":[]}'
    model = CheckpointedDeepSeek(job_id, fake, Factory)
    model.complete_json(system_prompt="x", user_prompt=req)
    with Factory() as db:
        job = db.get(OperationJob, job_id)
        job.pause_requested = True
        db.commit()
    with pytest.raises(ComparisonPause):
        model.complete_json(system_prompt="x", user_prompt=req)
    assert fake.calls == 1


def test_total_budget_is_persistent_across_restart(database, monkeypatch):
    Factory, job_id = database
    fake = Spy()
    monkeypatch.setattr(settings, "requirements_llm_max_calls", 1)
    CheckpointedDeepSeek(job_id, fake, Factory).complete_json(
        system_prompt="x", user_prompt='{"znp_atom":{"id":"a1"}}',
    )
    with pytest.raises(ComparisonInterrupted):
        CheckpointedDeepSeek(job_id, fake, Factory).complete_json(
            system_prompt="x", user_prompt='{"znp_atom":{"id":"a2"}}',
        )
    assert fake.calls == 1


def test_prompt_or_model_change_invalidates_replay(database, monkeypatch):
    Factory, job_id = database
    fake = Spy()
    original = request_fingerprint("system", '{"znp_atom":{}}')
    assert original != request_fingerprint("system-v2", '{"znp_atom":{}}')
    monkeypatch.setattr(settings, "deepseek_max_tokens", 12001)
    assert original != request_fingerprint("system", '{"znp_atom":{}}')


def test_compact_and_strict_flags_change_checkpoint_fingerprint(monkeypatch):
    from app.requirements.checkpoints import request_fingerprint
    base = request_fingerprint("system", '{"znp_atom":{}}')
    monkeypatch.setattr(settings, "requirements_evidence_id_mode", True)
    compact = request_fingerprint("system", '{"znp_atom":{}}')
    assert compact != base
    monkeypatch.setattr(settings, "deepseek_strict_evidence_tools", True)
    strict = request_fingerprint("system", '{"znp_atom":{}}')
    assert strict != compact


def test_restart_requeues_safe_job_and_pauses_uncertain_one(database, monkeypatch):
    Factory, first_id = database
    with Factory() as db:
        first = db.get(OperationJob, first_id)
        first.status = "running"
        second = OperationJob(
            project_id=first.project_id, kind="comparison",
            status="running", stage="verifying",
            params={"pair_ids":[]}, message="old",
        )
        db.add(second)
        db.flush()
        second_id = second.id
        db.add(ComparisonLLMCheckpoint(
            job_id=second_id, fingerprint="a"*64, phase="matching",
            status="sent", attempts=1,
        ))
        db.commit()

    monkeypatch.setattr(operations, "SessionLocal", Factory)
    monkeypatch.setattr(settings, "database_url", "sqlite:///recovery-test.db")
    submitted = []
    monkeypatch.setattr(
        operations._workers, "submit",
        lambda callback, ident: submitted.append(ident),
    )
    operations.recover_pending_operations()

    with Factory() as db:
        assert db.get(OperationJob, first_id).status == "queued"
        assert db.get(OperationJob, second_id).status == "paused"
    assert submitted == [first_id]


def test_targeted_truncation_reuses_failure_to_split_without_repaying(database):
    from app.ai.deepseek import DeepSeekResponseError

    Factory, job_id = database

    class Model:
        def __init__(self):
            self.calls = 0

        def complete_json(self, *, system_prompt, user_prompt):
            import json
            self.calls += 1
            count = len(json.loads(user_prompt)["znp_candidates"])
            if count > 2:
                raise DeepSeekResponseError(
                    "DeepSeek response was truncated; increase DEEPSEEK_MAX_TOKENS."
                )
            return {"links": []}

    import json
    provider = Model()
    full = json.dumps({
        "phase": "targeted", "tu_requirement": {"id": "t1"},
        "znp_candidates": [{"atom_id": f"a{i}"} for i in range(4)],
    })
    small = json.dumps({
        "phase": "targeted", "tu_requirement": {"id": "t1"},
        "znp_candidates": [{"atom_id": f"a{i}"} for i in range(2)],
    })
    with pytest.raises(DeepSeekResponseError, match="truncated"):
        CheckpointedDeepSeek(job_id, provider, Factory).complete_json(
            system_prompt="targeted", user_prompt=full,
        )
    assert provider.calls == 1
    # A restarted worker can safely split without paying for full batch again.
    with pytest.raises(DeepSeekResponseError, match="truncated"):
        CheckpointedDeepSeek(job_id, provider, Factory).complete_json(
            system_prompt="targeted", user_prompt=full,
        )
    assert provider.calls == 1
    result = CheckpointedDeepSeek(job_id, provider, Factory).complete_json(
        system_prompt="targeted", user_prompt=small,
    )
    assert result == {"links": []}
    assert provider.calls == 2
    assert checkpoint_counts(job_id, Factory)["saved"] == 1
    assert checkpoint_counts(job_id, Factory)["uncertain"] == 0


def test_cancel_takes_precedence_over_pause_even_for_cached_response(database):
    from app.requirements.checkpoints import ComparisonCancelled

    Factory, job_id = database
    with Factory() as db:
        job = db.get(OperationJob, job_id)
        job.cancel_requested = True
        job.pause_requested = True
        db.commit()
    provider = Spy()
    with pytest.raises(ComparisonCancelled):
        CheckpointedDeepSeek(job_id, provider, Factory).complete_json(
            system_prompt="x", user_prompt='{"znp_atom":{"id":"a1"}}',
        )
    assert provider.calls == 0



def test_disabled_flags_reuse_checkpoint_written_before_id_mode(database, monkeypatch):
    """Old `dev` fingerprints must remain byte-for-byte compatible."""
    import hashlib
    import json

    Factory, job_id = database
    monkeypatch.setattr(settings, "requirements_evidence_id_mode", False)
    monkeypatch.setattr(settings, "deepseek_strict_evidence_tools", False)
    system_prompt = "matching legacy"
    user_prompt = '{"znp_atom":{"id":"z1"},"tu_candidates":[]}'
    previous_version_identity = {
        "schema": "comparison-llm-v1",
        "model": settings.deepseek_model,
        "thinking": settings.deepseek_thinking,
        "max_tokens": settings.deepseek_max_tokens,
        "system": system_prompt,
        "user": user_prompt,
    }
    legacy_fingerprint = hashlib.sha256(json.dumps(
        previous_version_identity, sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    assert request_fingerprint(system_prompt, user_prompt) == legacy_fingerprint

    original_response = {"links": [], "search_more": False}
    with Factory() as db:
        db.add(ComparisonLLMCheckpoint(
            job_id=job_id, fingerprint=legacy_fingerprint,
            phase="matching", status="succeeded",
            attempts=1, response=original_response,
        ))
        db.commit()

    fake = Spy()
    assert CheckpointedDeepSeek(job_id, fake, Factory).complete_json(
        system_prompt=system_prompt, user_prompt=user_prompt,
    ) == original_response
    assert fake.calls == 0
    assert checkpoint_counts(job_id, Factory)["saved"] == 1


def test_strict_flag_alone_keeps_old_fingerprint_if_id_mode_disabled(monkeypatch):
    monkeypatch.setattr(settings, "requirements_evidence_id_mode", False)
    monkeypatch.setattr(settings, "deepseek_strict_evidence_tools", False)
    first = request_fingerprint("system", '{"znp_atom":{"id":"z1"}}')
    monkeypatch.setattr(settings, "deepseek_strict_evidence_tools", True)
    # Without ID mode this flag has no impact on the request sent to DeepSeek.
    assert request_fingerprint("system", '{"znp_atom":{"id":"z1"}}') == first
