"""Two-call Koktal probe using the production matching prompt and validators.

The input is the private, saved full-pair response. Default mode uses MockTransport.
No extraction, verify_all, or production protection is changed.
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from pydantic import SecretStr

from app.ai.deepseek import DeepSeekClient, DeepSeekConnectionError
from app.ai.embeddings import EmbeddingService
from app.core.config import Settings
from app.requirements.retrieval import RequirementRetriever
from app.requirements.schemas import RequirementStatus, TURequirement, ZNPClause
from app.requirements.source_coverage import apply_source_coverage_guard
from app.requirements.tools import tool_validate_quotes
from app.requirements.znp_first import ZnPFirstAgent


PDF_SHA256 = {
    "tu": "f149d0c70f2a60a964e10a13aa0453d0119188e6a4a85be47f7fad8898eec5cc",
    "znp": "02fdac64693b1ffab81543ecee4059d6e9b308af68d67125a132f2ad4c2eabb0",
}


class SingleAttemptHTTP:
    """Hard cap and no transport retry, even for ambiguous network failures."""

    def __init__(self, client):
        self.client = client
        self.attempts = 0
        self.requests = []
        self.responses = []

    def post(self, url, *, json, headers, timeout):
        if self.attempts >= 2:
            raise RuntimeError("Probe limit: two HTTP attempts total")
        self.attempts += 1
        self.requests.append(json)
        try:
            response = self.client.post(url, json=json, headers=headers, timeout=timeout)
            self.responses.append({"status_code": response.status_code,
                                   "body": response.text})
            return response
        except httpx.RequestError as exc:
            raise DeepSeekConnectionError(
                f"Probe stopped after one network failure ({type(exc).__name__}); no retry"
            ) from exc


def _response(raw):
    if "checked_requirements" in raw:
        return raw
    if "result" in raw:
        return _response(raw["result"])
    if "run" in raw:
        return _response(raw["run"])
    raise ValueError("Saved response has no checked_requirements")


def _selected_pairs(raw):
    response = _response(raw)
    trace = response["trace_info"]
    coverage = trace["source_coverage"]
    if coverage["complete"] is not False:
        raise ValueError("Expected incomplete ZnP source coverage")
    requirements = [TURequirement.model_validate(item["requirement"])
                    for item in response["checked_requirements"]]
    atoms = [ZNPClause.model_validate({**item, "id": item["atom_id"]})
             for item in trace["znp_atom_results"]]
    tu = next((item for item in requirements if item.clause_number == "3"), None)
    znp = {item.clause_number: item for item in atoms}
    if tu is None or "8" not in znp or "9" not in znp:
        raise ValueError("Koktal TU 3 / ZnP 8, 9 not found in saved result")
    for item in (tu, znp["8"], znp["9"]):
        if not item.sources or not all(
            source.get("fragment_id") and source.get("text") in item.text
            for source in item.sources
        ):
            raise ValueError("Selected item has unverified source ownership")
    return requirements, [("possible_substation", tu, znp["8"]),
                          ("negative_station_power", tu, znp["9"])], coverage


def run_probe(raw, transport, *, dry_run=True, config=None):
    requirements, pairs, coverage = _selected_pairs(raw)
    config = config or Settings(
        _env_file=None, deepseek_api_key=SecretStr("offline-probe"),
        deepseek_max_tokens=1024,
    )
    agent = ZnPFirstAgent(
        ai_client=DeepSeekClient(config=config, http_client=transport),
        embedder=EmbeddingService(enabled=False),
    )
    retriever = RequirementRetriever(
        requirements, EmbeddingService(enabled=False), batch_size=len(requirements),
    )
    results = []
    for label, tu, atom in pairs:
        ranked = retriever.search(atom)["candidates"]
        candidate = next((row for row in ranked if row["requirement_id"] == tu.id), None)
        if candidate is None:
            raise ValueError("Selected TU requirement absent from retrieval")
        reply = agent._judge(atom, [candidate], [])
        if not isinstance(reply, dict) or not isinstance(reply.get("links"), list):
            raise ValueError("Model reply has no links list; probe stopped")
        request = transport.requests[-1]
        messages = request["messages"]
        prompt_bytes = sum(len(message["content"].encode("utf-8")) for message in messages)
        rejected = Counter()
        validated = []
        for proposal in reply["links"]:
            if not isinstance(proposal, dict) or proposal.get("requirement_id") != tu.id:
                rejected["unknown_candidate"] += 1
                continue
            quotes = tool_validate_quotes(
                tu.text, proposal.get("tu_quote"), atom.text,
                proposal.get("znp_quote"),
            ) if (isinstance(proposal.get("tu_quote"), str)
                  and isinstance(proposal.get("znp_quote"), str)) else None
            link = agent._validated_link(
                atom, tu, proposal, candidate["similarity_score"], rejected,
            )
            validated.append({
                "quote_checks": quotes,
                "accepted": link is not None,
                "relation_after_validation": link["relation"] if link else None,
                "warnings": link["warnings"] if link else [],
                "param_check": link["param_check"] if link else None,
                "scope_consistent": link["scope_consistent"] if link else None,
            })
        rehearsal = None
        if dry_run:
            # A deliberately optimistic local proposal tests the guards. It is
            # never attributed to DeepSeek or used as a compliance finding.
            tu_quote = next((line.strip() for source in tu.sources
                             for line in source["text"].splitlines()
                             if "10 МВА" in line), tu.sources[0]["text"])
            marker = "ОРУ" if atom.clause_number == "8" else "8,6"
            znp_quote = next((line.strip() for source in atom.sources
                              for line in source["text"].splitlines()
                              if marker in line), atom.sources[0]["text"])
            sample = {"requirement_id": tu.id, "relation": "supports",
                      "tu_quote": tu_quote, "znp_quote": znp_quote,
                      "confidence": 0.95, "reasoning": "offline guard rehearsal"}
            quote_checks = tool_validate_quotes(tu.text, tu_quote, atom.text, znp_quote)
            checked = agent._validated_link(
                atom, tu, sample, candidate["similarity_score"], Counter(),
            )
            rehearsal = {
                "synthetic_proposal": True,
                "quote_checks": quote_checks,
                "accepted": checked is not None,
                "relation_after_validation": checked["relation"] if checked else None,
                "param_check": checked["param_check"] if checked else None,
                "scope_consistent": checked["scope_consistent"] if checked else None,
                "warnings": checked["warnings"] if checked else [],
            }
        results.append({
            "case": label,
            "tu_clause": tu.clause_number, "tu_id": tu.id,
            "tu_sources": [source["fragment_id"] for source in tu.sources],
            "tu_warnings": tu.warnings,
            "znp_clause": atom.clause_number, "znp_id": atom.id,
            "znp_sources": [source["fragment_id"] for source in atom.sources],
            "znp_warnings": atom.warnings,
            "request_sha256": hashlib.sha256(
                json.dumps(request, ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
            "prompt_utf8_bytes": prompt_bytes,
            "input_token_upper_estimate": prompt_bytes + 200,
            "max_completion_tokens": request["max_tokens"],
            "proposed_links": len(reply["links"]),
            "validated_links": validated,
            "rejection_counts": dict(rejected),
            "local_validator_rehearsal": rehearsal,
        })
    sample = SimpleNamespace(
        status=RequirementStatus.fulfilled,
        automatic_status=RequirementStatus.fulfilled,
        confidence_score=1.0, reasoning="", validation_details=None,
    )
    guarded = SimpleNamespace(checked_requirements=[sample], stats={}, trace_info={})
    apply_source_coverage_guard(guarded, coverage)
    if sample.status != RequirementStatus.needs_review:
        raise AssertionError("Incomplete source coverage did not block fulfilled")
    return {
        "dry_run": dry_run,
        "api_calls": 0 if dry_run else transport.attempts,
        "mock_calls": transport.attempts if dry_run else 0,
        "source_coverage_complete": coverage["complete"],
        "unclassified_fragments": coverage["unclassified_count"],
        "guarded_status": sample.status.value,
        "pairs": results,
        "input_token_upper_estimate": sum(
            item["input_token_upper_estimate"] for item in results
        ),
        "total_token_upper_estimate": sum(
            item["input_token_upper_estimate"] + item["max_completion_tokens"]
            for item in results
        ),
    }


def _verify_pdf(path, expected):
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != PDF_SHA256[expected]:
        raise ValueError(f"Unexpected {expected.upper()} PDF SHA-256")
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saved-result", type=Path, required=True)
    parser.add_argument("--tu-pdf", type=Path, required=True)
    parser.add_argument("--znp-pdf", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--private-output", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-paid-two-calls", action="store_true")
    args = parser.parse_args()
    if args.live and not args.confirm_paid_two_calls:
        parser.error("Live mode requires --confirm-paid-two-calls")
    hashes = {name: _verify_pdf(path, name) for name, path in (
        ("tu", args.tu_pdf), ("znp", args.znp_pdf),
    )}
    raw = json.loads(args.saved_result.read_text(encoding="utf-8"))
    if args.live:
        config = Settings(deepseek_max_tokens=1024)
        if not config.deepseek_api_key.get_secret_value().strip():
            parser.error("DEEPSEEK_API_KEY is missing")
        client = httpx.Client(transport=httpx.HTTPTransport(retries=0))
    else:
        config = Settings(
            _env_file=None, deepseek_api_key=SecretStr("offline-probe"),
            deepseek_base_url="https://offline.invalid", deepseek_max_tokens=1024,
        )
        def fake(_request):
            return httpx.Response(200, json={"choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"links":[],"search_more":false}'},
            }]})
        client = httpx.Client(transport=httpx.MockTransport(fake))
    with client:
        transport = SingleAttemptHTTP(client)
        try:
            report = run_probe(raw, transport, dry_run=not args.live, config=config)
        finally:
            if args.private_output:
                args.private_output.write_text(json.dumps(
                    {"requests": transport.requests, "responses": transport.responses},
                    ensure_ascii=False, indent=2,
                ), encoding="utf-8")
    report["pdf_sha256"] = hashes
    result = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(result + "\n", encoding="utf-8")
    print(result)


if __name__ == "__main__":
    main()
