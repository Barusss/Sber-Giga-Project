"""Private runner integration; test data/checkpoints are never committed."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


@pytest.mark.skipif(not Path("/large/run.py").is_file(), reason="Local diagnostic runner unavailable")
def test_post_http_error_is_failed_then_cached_resume_does_not_repay(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("p17_diagnostic_runner", "/large/run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = json.loads(Path("/large/paid-plan.json").read_text())
    plan.pop("plan_sha256")
    plan["jobs"] = plan["jobs"][:2]
    data = {r["id"]: r for r in json.loads(Path("/large/dataset.private.json").read_text())}
    monkeypatch.setattr(module, "P", tmp_path)
    validate = module.validate_reply

    def fail_validation(*args):
        raise ValueError("post-HTTP validation sentinel")

    monkeypatch.setattr(module, "validate_reply", fail_validation)
    with pytest.raises(ValueError, match="post-HTTP validation sentinel"):
        module.execute(plan, data, "recovery", 2, 100000, mock=True)
    directory = tmp_path / "recovery"
    state = json.loads((directory / "state.json").read_text())
    assert state["attempts"] == 1 and not state["results"]
    assert state["stop"]["stage"] == "post_http_validation"
    assert state["stop"]["resume_from_saved_response"] is True
    with sqlite3.connect(directory / "checkpoints.private.sqlite") as db:
        assert db.execute("select status, stage from operation_jobs").fetchone() == (
            "failed", "post_http_validation",
        )
        original = db.execute("select id,fingerprint,status,attempts,response from comparison_llm_checkpoints").fetchone()
        assert original[2:4] == ("succeeded", 1)

    monkeypatch.setattr(module, "validate_reply", validate)
    recovered = module.execute(plan, data, "recovery", 2, 100000, mock=True, cached_only=True)
    assert recovered["attempts"] == 1 and recovered["saved"] == 1
    assert recovered["tokens"] == state["tokens"]
    with sqlite3.connect(directory / "checkpoints.private.sqlite") as db:
        assert db.execute("select status from operation_jobs").fetchone()[0] == "paused"
        assert db.execute("select id,fingerprint,status,attempts,response from comparison_llm_checkpoints").fetchone() == original
    finished = module.execute(plan, data, "recovery", 2, 100000, mock=True)
    assert finished["attempts"] == 2 and finished["remaining"] == 0
