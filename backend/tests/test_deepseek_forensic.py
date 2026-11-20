"""Forensic diagnostics tests: real two-file parsing, mocked provider, redaction.

These are NOT a substitute for a paid run on the user's two real documents.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from docx import Document as WordDocument

from app.ai import diagnostics
from app.ai.deepseek import DeepSeekClient, DeepSeekResponseError
from app.core.config import Settings
from scripts.diagnose_deepseek_pair import run_pair


MARKER = "CONFIDENTIAL_SOURCE_SENTINEL_9865"
SECRET = "diagnostic-test-api-key-not-for-logs"


def pair(tmp_path: Path):
    tu = WordDocument()
    tu.add_heading("Технические условия", 1)
    tu.add_paragraph(f"1.1. Для КЛ-1 предусмотреть кабель 10 кВ. {MARKER}")
    tu.add_paragraph("1.2. Для КЛ-1 предусмотреть сечение 240 мм².")
    tu_path = tmp_path / "tu.docx"
    tu.save(tu_path)

    znp = WordDocument()
    znp.add_heading("Задание на проектирование", 1)
    znp.add_paragraph("2.1. Для КЛ-1 предусмотреть кабель 10 кВ.")
    znp.add_paragraph("2.2. Для КЛ-1 предусмотреть сечение 240 мм².")
    znp_path = tmp_path / "znp.docx"
    znp.save(znp_path)
    return tu_path, znp_path


class NoEvidenceModel:
    def complete_json(self, *, system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        if "clause_text" in payload:
            return {"atoms": []}
        if "obligation" in payload:
            return {
                "covered": False, "object_consistent": False,
                "evidence_ids": [], "reasoning": "No evidence",
            }
        if "candidates" in payload and "requirement" in payload:
            return {"conflicts": [], "uncertain_ids": []}
        return {"links": [], "search_more": False}


def events(root: Path):
    return [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def test_two_real_docx_files_dry_run_never_calls_model(tmp_path):
    tu, znp = pair(tmp_path)

    class Never:
        def complete_json(self, **kwargs):
            pytest.fail("Dry run must not make API calls")

    with diagnostics.diagnostic_run(tmp_path / "logs", strict=True):
        directory = Path(diagnostics.trace_directory())
        summary = run_pair(tu, znp, dry_run=True, client=Never())
    assert summary["tu_clauses"] >= 1
    assert summary["znp_atoms"] >= 1
    assert summary["deepseek_calls"] == 0
    names = {entry["event"] for entry in events(directory)}
    assert "document.parsed" in names
    assert "extraction.completed" in names


def test_full_two_file_pipeline_logs_all_phases_without_leaking_text(tmp_path):
    tu, znp = pair(tmp_path)
    with diagnostics.diagnostic_run(tmp_path / "logs", strict=True):
        directory = Path(diagnostics.trace_directory())
        summary = run_pair(
            tu, znp, client=NoEvidenceModel(), no_embeddings=True,
        )
    assert summary["tu_requirements"] >= 1
    assert summary["znp_atoms"] >= 1
    names = {entry["event"] for entry in events(directory)}
    assert "pipeline.inputs_ready" in names
    assert "matching.retrieval" in names
    assert "ledger.request" in names
    assert "targeted.batch.begin" in names
    assert "matching.finished" in names
    log_text = (directory / "events.jsonl").read_text(encoding="utf-8")
    assert MARKER not in log_text
    assert SECRET not in log_text
    assert not (directory / "private").exists()


def test_provider_finish_reason_length_reports_token_usage_and_request_id(tmp_path):
    def handler(request):
        return httpx.Response(
            200,
            headers={"x-request-id": "deepseek-test-rid-123"},
            json={
                "choices": [{
                    "message": {"content": '{"some":"unfinished"}'},
                    "finish_reason": "length",
                }],
                "usage": {"prompt_tokens": 1500, "completion_tokens": 3000,
                          "total_tokens": 4500},
            },
        )
    settings = Settings(_env_file=None, deepseek_api_key=SECRET)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = DeepSeekClient(config=settings, http_client=http)
        with diagnostics.diagnostic_run(tmp_path / "logs", strict=True):
            directory = Path(diagnostics.trace_directory())
            with pytest.raises(DeepSeekResponseError, match="truncated"):
                client.complete_json(
                    system_prompt="Return JSON", user_prompt=MARKER,
                )
    items = events(directory)
    parsed = next(item for item in items if item["event"] == "provider.response.parsed")
    assert parsed["finish_reason"] == "length"
    assert parsed["completion_tokens"] == 3000
    assert parsed["prompt_tokens"] == 1500
    http_event = next(item for item in items if item["event"] == "provider.http.response")
    assert http_event["request_id"] == "deepseek-test-rid-123"
    assert "finish_reason_length" in [item.get("code") for item in items]
    raw = (directory / "events.jsonl").read_text(encoding="utf-8")
    assert SECRET not in raw
    assert MARKER not in raw


def test_private_opt_in_saves_prompt_and_response_but_not_api_key(tmp_path):
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        })
    settings = Settings(_env_file=None, deepseek_api_key=SECRET)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        ai = DeepSeekClient(config=settings, http_client=http)
        with diagnostics.diagnostic_run(
            tmp_path / "logs", capture_payloads=True, strict=True,
        ):
            directory = Path(diagnostics.trace_directory())
            ai.complete_json(system_prompt="Return JSON", user_prompt=MARKER)
    private_text = "\n".join(
        file.read_text(encoding="utf-8") for file in
        sorted((directory / "private").glob("*.json"))
    )
    assert MARKER in private_text
    assert SECRET not in private_text
    assert MARKER not in (directory / "events.jsonl").read_text(encoding="utf-8")


def test_invalid_json_error_records_position_not_raw_content(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"choices": [{
            "message": {"content": '{invalid-token' }, "finish_reason": "stop",
        }]},
    ))) as http:
        ai = DeepSeekClient(
            config=Settings(_env_file=None, deepseek_api_key=SECRET), http_client=http,
        )
        with diagnostics.diagnostic_run(tmp_path / "logs", strict=True):
            directory = Path(diagnostics.trace_directory())
            with pytest.raises(DeepSeekResponseError, match="invalid JSON"):
                ai.complete_json(system_prompt="json", user_prompt=MARKER)
    entry = next(row for row in events(directory) if row["event"] == "provider.json.invalid")
    assert entry["error_line"] == 1
    assert isinstance(entry["error_position"], int)
    assert MARKER not in (directory / "events.jsonl").read_text(encoding="utf-8")
