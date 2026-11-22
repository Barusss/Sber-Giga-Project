"""Tests for the offline audit; no backend, network, or model required."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_comparison_result.py"
SPEC = importlib.util.spec_from_file_location("audit_comparison_result", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def result(*, complete=False, status="needs_review", sources=None, links=None):
    return {
        "result": {
            "checked_requirements": [
                {"id": "tu1", "status": status, "automatic_status": status,
                 "is_confirmed_by_human": False}
            ],
            "stats": {status: 1, "total": 1},
            "trace_info": {
                "source_coverage": {"complete": complete},
                "znp_atom_results": [{
                    "atom_id": "z1",
                    "text": "9. Мощность 8,6 МВт",
                    "sources": sources or [{
                        "fragment_id": "f1", "start_offset": 4,
                        "end_offset": 13, "text": "8,6 МВт!!"
                    }],
                    "links": links or [],
                }],
            },
        },
    }


def test_incomplete_blocks_auto_fulfilled():
    report = MODULE.audit(result(status="fulfilled"))
    assert not report["passed"]
    assert "Auto-fulfilled" in report["errors"][0]


def test_human_confirmed_status_is_not_falsely_rejected():
    value = result(status="fulfilled")
    value["result"]["checked_requirements"][0]["is_confirmed_by_human"] = True
    assert MODULE.audit(value)["passed"]


def test_valid_incomplete_review_passes():
    report = MODULE.audit(result(), expected_tu=1, expected_znp=1)
    assert report["passed"]
    assert report["semantic_precision"] == "UNKNOWN"


def test_bad_span_fails():
    report = MODULE.audit(result(sources=[{
        "fragment_id": "f1", "start_offset": 0, "end_offset": 2,
        "text": "МВт"
    }]))
    assert not report["passed"]
    assert any("source span" in error for error in report["errors"])


def test_supports_with_warning_fails():
    report = MODULE.audit(result(complete=True, links=[{
        "link_id": "tu1:z1", "relation": "supports", "quote": "8,6 МВт",
        "warnings": ["table_structure_unverified"]
    }]))
    assert not report["passed"]


def test_supports_with_outside_quote_fails():
    report = MODULE.audit(result(complete=True, links=[{
        "link_id": "tu1:z1", "relation": "supports",
        "quote": "9 баллов", "warnings": []
    }]))
    assert not report["passed"]


def test_mixed_location_and_power_fails():
    value = result()
    value["result"]["trace_info"]["znp_atom_results"][0]["text"] = (
        "7. Местоположение. 9. 8,6 МВт"
    )
    report = MODULE.audit(value)
    assert not report["passed"]


def test_stats_mismatch_fails():
    value = result()
    value["result"]["stats"]["total"] = 100
    assert not MODULE.audit(value)["passed"]


def test_correct_supports_without_warning_passes():
    value = result(complete=True, links=[{
        "link_id": "tu1:z1", "relation": "supports",
        "quote": "8,6 МВт", "warnings": [],
        "scope_consistent": True,
        "param_check": {"params_match": True}
    }], sources=[{
        "fragment_id": "f1", "start_offset": 5,
        "end_offset": 12, "text": "8,6 МВт"
    }])
    report = MODULE.audit(value)
    assert report["passed"]
    assert report["accepted_supports"] == 1
