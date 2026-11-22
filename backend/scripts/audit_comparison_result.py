"""Offline safety audit of a saved TU/ZnP comparison response.

No database, network, or LLM access. Does NOT measure semantic correctness.
Usage:
    python backend/scripts/audit_comparison_result.py run.json --expected-tu 9 --expected-znp 28
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re


def _response(value):
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    if isinstance(value.get("checked_requirements"), list):
        return value
    if isinstance(value.get("result"), dict):
        return _response(value["result"])
    if isinstance(value.get("run"), dict):
        return _response(value["run"])
    raise ValueError("No checked_requirements found in result")


def _normal(text):
    return " ".join(str(text or "").split()).casefold()


def audit(raw, expected_tu=None, expected_znp=None):
    response = _response(raw)
    items = response["checked_requirements"]
    trace = response.get("trace_info") or {}
    coverage = trace.get("source_coverage") or {}
    atoms = trace.get("znp_atom_results") or []
    errors, notes = [], []
    statuses = Counter(item.get("status") for item in items)

    if expected_tu is not None and len(items) != expected_tu:
        errors.append(f"TU count: expected {expected_tu}, got {len(items)}")
    if expected_znp is not None and len(atoms) != expected_znp:
        errors.append(f"ZnP atom count: expected {expected_znp}, got {len(atoms)}")

    for key in ("fulfilled", "missing", "needs_review"):
        recorded = (response.get("stats") or {}).get(key)
        if recorded is not None and recorded != statuses[key]:
            errors.append(f"stats.{key}: {recorded} != actual {statuses[key]}")
    recorded_total = (response.get("stats") or {}).get("total")
    if recorded_total is not None and recorded_total != len(items):
        errors.append(f"stats.total: {recorded_total} != {len(items)}")

    if coverage.get("complete") is False:
        for item in items:
            human = item.get("is_confirmed_by_human") is True
            if not human and (
                item.get("status") == "fulfilled"
                or item.get("automatic_status") == "fulfilled"
            ):
                errors.append(
                    f"Auto-fulfilled despite incomplete ZnP: {item.get('id')}"
                )
        notes.append("Source coverage incomplete: automatic fulfilled forbidden")
    elif "complete" not in coverage:
        notes.append("Source coverage flag absent; completeness not established")

    valid_links = 0
    accepted_supports = 0
    for atom in atoms:
        sources = atom.get("sources") or []
        for source in sources:
            if not isinstance(source, dict):
                errors.append(f"Invalid source in atom {atom.get('atom_id')}")
                continue
            start = source.get("start_offset")
            end = source.get("end_offset")
            text = source.get("text")
            if not (
                isinstance(start, int) and isinstance(end, int)
                and isinstance(text, str) and 0 <= start < end
                and end - start == len(text)
            ):
                errors.append(
                    f"Invalid source span in atom {atom.get('atom_id')}"
                )
        for link in atom.get("links") or []:
            valid_links += 1
            if link.get("relation") != "supports":
                continue
            accepted_supports += 1
            if link.get("warnings"):
                errors.append(
                    f"Supports with warnings: {link.get('link_id')}"
                )
            if link.get("scope_consistent") is False:
                errors.append(
                    f"Supports with inconsistent scope: {link.get('link_id')}"
                )
            params = link.get("param_check")
            if isinstance(params, dict) and params.get("params_match") is False:
                errors.append(
                    f"Supports with parameter mismatch: {link.get('link_id')}"
                )
            quote = _normal(link.get("quote"))
            if not quote:
                errors.append(f"Supports without ZnP quote: {link.get('link_id')}")
            elif sources and not any(
                quote in _normal(s.get("text"))
                for s in sources if isinstance(s, dict)
            ):
                errors.append(
                    f"Supports quote outside atom sources: {link.get('link_id')}"
                )

    # Distinguish the known structural regression from semantic evaluation.
    for atom in atoms:
        atom_text = _normal(atom.get("text"))
        is_location = re.search(
            r"местоположени|месторасположени|местонахождени",
            atom_text,
        )
        has_power = re.search(
            r"8[.,]6\s*(?:мвт|mw)\b", atom_text,
        )
        if is_location and has_power:
            errors.append(
                f"Koktal: location atom includes 8.6 MW: {atom.get('atom_id')}"
            )

    if trace.get("model_failure_kind"):
        notes.append(f"Model error: {trace['model_failure_kind']}")
    if trace.get("degraded"):
        notes.append("Degraded comparison: interpret results cautiously")
    if trace.get("matching_limit_reached"):
        notes.append("Matching request limit reached")

    return {
        "passed": not errors,
        "items": len(items),
        "znp_atoms_in_trace": len(atoms),
        "statuses": dict(statuses),
        "source_coverage_complete": coverage.get("complete"),
        "accepted_links": valid_links,
        "accepted_supports": accepted_supports,
        "errors": errors,
        "notes": notes,
        "semantic_precision": "UNKNOWN",
        "semantic_recall": "UNKNOWN",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_file", type=Path)
    parser.add_argument("--expected-tu", type=int)
    parser.add_argument("--expected-znp", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        raw = json.loads(args.json_file.read_text(encoding="utf-8"))
        report = audit(raw, args.expected_tu, args.expected_znp)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"Invalid input: {exc}\n")
    result = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(result + "\n", encoding="utf-8")
    print(result)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
