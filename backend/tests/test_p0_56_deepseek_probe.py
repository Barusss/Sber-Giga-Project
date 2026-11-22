import httpx
import pytest

from app.ai.deepseek import DeepSeekAPIError, DeepSeekConnectionError, DeepSeekResponseError
from scripts.p0_56_deepseek_probe import SingleAttemptHTTP, run_probe


def _snapshot():
    tu = {
        "id": "tu-3", "clause_number": "3", "category": "power",
        "text": "Трансформатор 10 МВА с ОРУ-110 кВ",
        "sources": [{"fragment_id": "tu-frag", "page": 1,
                     "start_offset": 0, "end_offset": 36,
                     "text": "Трансформатор 10 МВА с ОРУ-110 кВ"}],
        "warnings": ["table_structure_unverified"],
    }
    atoms = [
        {"atom_id": "z-8", "clause_number": "8", "text": "ПС-110/6 кВ с ОРУ-110 кВ",
         "sources": [{"fragment_id": "z-frag-8", "page": 1,
                      "start_offset": 0, "end_offset": 25,
                      "text": "ПС-110/6 кВ с ОРУ-110 кВ"}], "warnings": []},
        {"atom_id": "z-9", "clause_number": "9", "text": "Мощность станции 8,6 МВт",
         "sources": [{"fragment_id": "z-frag-9", "page": 1,
                      "start_offset": 0, "end_offset": 25,
                      "text": "Мощность станции 8,6 МВт"}], "warnings": []},
    ]
    return {"result": {"checked_requirements": [{"requirement": tu}],
                       "trace_info": {"source_coverage": {"complete": False,
                           "unclassified_count": 1, "schema": "test",
                           "total_fragments": 1, "atoms": 2,
                           "by_role": {}, "unclassified_by_block_type": {},
                           "excluded_by_reason": {}, "review_hint_counts": {},
                           "unclassified_fragment_ids": []},
                           "znp_atom_results": atoms}}}


def test_dry_run_builds_two_production_requests_without_network():
    requests = []

    def fake(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": '{"links":[],"search_more":false}'}}]})

    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        report = run_probe(_snapshot(), SingleAttemptHTTP(client), dry_run=True)
    assert len(requests) == 2
    assert all("одно положение ЗнП" in req.content.decode("utf-8") for req in requests)
    assert [pair["znp_clause"] for pair in report["pairs"]] == ["8", "9"]
    assert report["api_calls"] == 0
    assert report["mock_calls"] == 2
    assert report["guarded_status"] == "needs_review"
    for pair in report["pairs"]:
        rehearsal = pair["local_validator_rehearsal"]
        assert rehearsal["quote_checks"]["is_valid"]
        assert rehearsal["quote_checks"]["znp_quote_atomic"]
        assert rehearsal["relation_after_validation"] != "supports"


@pytest.mark.parametrize("kind,error", [
    ("http402", DeepSeekAPIError),
    ("network", DeepSeekConnectionError),
    ("read_timeout", DeepSeekConnectionError),
    ("invalid_json", DeepSeekResponseError),
])
def test_first_failure_is_not_retried(kind, error):
    attempts = []

    def fake(request):
        attempts.append(request)
        if kind == "network":
            raise httpx.ConnectError("offline", request=request)
        if kind == "read_timeout":
            raise httpx.ReadTimeout("offline", request=request)
        if kind == "http402":
            return httpx.Response(402, json={"error": "balance"})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": "not json"}}]})

    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        with pytest.raises(error):
            run_probe(_snapshot(), SingleAttemptHTTP(client), dry_run=False)
    assert len(attempts) == 1


def test_transport_refuses_third_http_attempt():
    attempts = []
    def fake(request):
        attempts.append(request)
        return httpx.Response(200, json={})
    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        transport = SingleAttemptHTTP(client)
        for _ in range(2):
            transport.post("https://offline.invalid", json={}, headers={}, timeout=1)
        with pytest.raises(RuntimeError, match="two HTTP attempts"):
            transport.post("https://offline.invalid", json={}, headers={}, timeout=1)
    assert len(attempts) == 2
