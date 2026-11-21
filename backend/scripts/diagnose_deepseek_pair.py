"""Reproduce the full TU/ZnP DeepSeek workflow on TWO LOCAL documents.

Runs the production parser, TU clause extraction, conservative/semantic ZnP
atomization and ZnPFirstAgent with targeted reverse retrieval. No database
writes; no other project or UI setup required. Real DeepSeek calls are made
unless --dry-run is specified. Never accepts an API key on the command line.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.ai import diagnostics
from app.ai.deepseek import DeepSeekClient
from app.ai.embeddings import EmbeddingService
from app.core.config import settings
from app.core.models import Fragment
from app.documents.parser import extract_blocks
from app.requirements.atomization import atomize_fragments
from app.requirements.extraction import extract_clauses, is_service_clause
from app.requirements.llm_tracking import TrackedRequirementsLLM
from app.requirements.coverage import build_obligations
from app.requirements.schemas import TURequirement, ZNPClause
from app.requirements.semantic_atomization import semantic_atomize, atomization_workload
from app.requirements.budget_planning import plan_full_run
from app.requirements.tools import classify_requirement_category
from app.requirements.znp_first import ZnPFirstAgent


def load_fragments(path: Path, kind: str):
    """Build real Fragment objects from PDF/DOCX, without database persistence."""
    if path.suffix.lower() not in {".pdf", ".docx"}:
        raise ValueError(f"{kind}: only .pdf and .docx files are supported")
    raw = path.read_bytes()
    doc_id = str(uuid5(NAMESPACE_URL, f"diagnostic:{kind}:{diagnostics.sha(raw)}"))
    diagnostics.record(
        "document.read", kind=kind, bytes=len(raw), sha=diagnostics.sha(raw),
        extension=path.suffix.lower(),
    )
    started = time.monotonic()
    blocks = extract_blocks(raw, path.suffix.lower())
    fragments = [
        Fragment(
            id=str(uuid5(NAMESPACE_URL, f"{doc_id}:{i}")),
            document_id=doc_id,
            page=block.page,
            position=block.position,
            text=block.text,
            block_type=block.block_type,
            section_path=block.section_path,
            container_id=block.container_id,
            bbox=block.bbox,
            extraction_method=block.extraction_method,
        )
        for i, block in enumerate(blocks, start=1)
    ]
    diagnostics.record(
        "document.parsed", kind=kind, blocks=len(blocks), fragments=len(fragments),
        chars=sum(len(item.text) for item in fragments),
        pages=max((item.page for item in fragments), default=0),
        methods=dict(Counter(block.extraction_method for block in blocks)),
        types=dict(Counter(block.block_type for block in blocks)),
        elapsed_ms=round((time.monotonic() - started) * 1000),
    )
    return doc_id, fragments


def run_pair(
    tu_path: Path,
    znp_path: Path,
    *,
    dry_run: bool = False,
    no_embeddings: bool = False,
    max_calls: int | None = None,
    targeted_calls: int | None = None,
    probe: bool = False,
    client=None,
):
    """Execute actual comparison components; client injectable for deterministic tests."""
    started = time.monotonic()
    tu_id, tu_fragments = load_fragments(tu_path, "tu")
    znp_id, znp_fragments = load_fragments(znp_path, "znp")

    tu_raw = [clause for clause in extract_clauses(tu_fragments)
              if not is_service_clause(clause.text)]
    znp_original = atomize_fragments(znp_id, znp_fragments)
    parent_scope_report = {
        "warnings": dict(Counter(
            warning for atom in znp_original for warning in atom.warnings
        )),
        "table_rows": sum(f.block_type == "table_row" for f in znp_fragments),
        "table_rows_with_container": sum(
            f.block_type == "table_row" and bool(f.container_id)
            for f in znp_fragments
        ),
        "distinct_table_containers": len({
            f.container_id for f in znp_fragments
            if f.block_type == "table_row" and f.container_id
        }),
        "atoms_with_parent_context": sum(
            bool(atom.context_sources) for atom in znp_original
        ),
    }
    diagnostics.record("extraction.parent_scope", **parent_scope_report)
    diagnostics.record(
        "extraction.completed", tu_requirements=len(tu_raw),
        znp_original_atoms=len(znp_original),
        tu_longest_chars=max((len(c.text) for c in tu_raw), default=0),
        znp_longest_chars=max((len(c.text) for c in znp_original), default=0),
        tu_total_chars=sum(len(c.text) for c in tu_raw),
        znp_total_chars=sum(len(c.text) for c in znp_original),
    )
    if not tu_raw or not znp_original:
        raise ValueError("No useful requirements/atoms: inspect parser and OCR stages")

    budget = plan_full_run(
        tu_clauses=tu_raw,
        znp_source_atoms=len(znp_original),
        atomization_eligible=atomization_workload(znp_original)["eligible"],
        max_calls=max_calls if max_calls is not None
            else settings.requirements_llm_max_calls,
        atomization_limit=settings.requirements_llm_atomization_calls,
        targeted_limit=targeted_calls if targeted_calls is not None
            else settings.requirements_llm_targeted_calls,
        matching_batches=5,
    )
    diagnostics.record("pipeline.budget_preflight", **budget.as_dict())
    if dry_run:
        return {
            "mode": "dry-run", "tu_clauses": len(tu_raw),
            "znp_atoms": len(znp_original),
            "deepseek_calls": 0,
            "parent_scope": parent_scope_report,
            "budget_plan": budget.as_dict(),
        }
    if budget.blockers:
        diagnostics.record(
            "pipeline.budget_preflight_refused", blockers=budget.blockers,
            no_paid_calls=True,
        )
        raise ValueError(budget.refusal_message())

    model = client if client is not None else DeepSeekClient()
    if probe:
        diagnostics.record("probe.start")
        probe_result = model.complete_json(
            system_prompt="Return one valid JSON object with ok=true.",
            user_prompt='{"probe":"health","expected":{"ok":true}}',
        )
        diagnostics.record(
            "probe.finished", response_type=type(probe_result).__name__,
            has_ok=probe_result.get("ok") is True,
        )

    ledger = TrackedRequirementsLLM(
        model,
        max_calls=budget.max_calls,
        atomization_limit=settings.requirements_llm_atomization_calls,
        targeted_limit=budget.targeted_limit,
    )
    diagnostics.record(
        "pipeline.start", max_calls=ledger.max_calls,
        phase_caps=ledger.phase_limits,
        embeddings_enabled=not no_embeddings,
    )
    semantic = semantic_atomize(znp_original, ledger)
    diagnostics.record("atomization.completed", **semantic.trace())

    by_tu_id = {fragment.id: fragment for fragment in tu_fragments}
    tu_items = []
    for index, clause in enumerate(tu_raw, start=1):
        requirement_id = str(uuid5(
            NAMESPACE_URL, f"diagnostic-tu:{tu_id}:{index}:{clause.text}"
        ))
        tu_items.append(TURequirement(
            id=requirement_id,
            clause_number=clause.clause_number,
            category=classify_requirement_category(clause.text),
            text=clause.text,
            fragment_id=clause.fragment_id,
            page=clause.page,
            fragment_text=clause.fragment_text,
            section_path=clause.section_path,
            sources=[{
                "fragment_id": fragment_id,
                "page": by_tu_id[fragment_id].page,
                "text": by_tu_id[fragment_id].text.strip(),
            } for fragment_id in clause.fragment_ids if fragment_id in by_tu_id],
        ))

    znp_items = []
    for atom in semantic.atoms:
        first = atom.sources[0]
        znp_items.append(ZNPClause(
            id=atom.id,
            clause_number=atom.clause_number,
            text=atom.text,
            fragment_id=first.fragment_id,
            page=first.page,
            fragment_text=first.text,
            section_path=atom.section_path,
            sources=[asdict(source) for source in atom.sources],
            context_sources=[asdict(source) for source in atom.context_sources],
            warnings=list(atom.warnings),
        ))
    diagnostics.record(
        "pipeline.inputs_ready", tu_requirements=len(tu_items),
        znp_atoms=len(znp_items), atomization_calls=semantic.model_calls,
    )

    agent = ZnPFirstAgent(
        ai_client=ledger, embedder=EmbeddingService(enabled=not no_embeddings),
    )
    ledger.configure_full_run_limits(
        atom_count=len(znp_items),
        obligation_count=sum(len(build_obligations(item)) for item in tu_items),
        requirement_count=len(tu_items),
        matching_batches=agent.max_batches,
    )

    def progress(stage: str, done: int, total: int, current: str | None):
        diagnostics.record(
            "pipeline.progress", stage=stage, completed=done, total=total,
            clause_sha=diagnostics.sha(current) if current else None,
        )

    try:
        result = agent.verify_all(tu_items, znp_items, progress_callback=progress)
    finally:
        diagnostics.record("pipeline.ledger_final", **ledger.trace())
        if client is None:
            model.close()

    info = result.trace_info
    stages = info.get("comparison_stages", {})
    report = {
        "mode": "live-or-injected",
        "budget_plan": budget.as_dict(),
        "tu_requirements": len(tu_items),
        "znp_atoms": len(znp_items),
        "parent_scope": parent_scope_report,
        "atomization": semantic.trace(),
        "usage": ledger.trace(),
        "comparison_stages": stages,
        "status_counts": result.stats,
        "model_failure_kind": info.get("model_failure_kind"),
        "model_failure_reason": info.get("model_failure_reason"),
        "degraded": info.get("degraded"),
        "targeted_truncated_responses": info.get("targeted_truncated_responses"),
        "targeted_incomplete_requirements": info.get("targeted_incomplete_requirements"),
        "rejected_links": info.get("rejected_links"),
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
    diagnostics.record(
        "pipeline.result", degraded=bool(report["degraded"]),
        statuses=result.stats,
        failure_kind=report["model_failure_kind"],
        truncated=report["targeted_truncated_responses"],
    )
    # Full evidence and source text may be inspected locally, only when
    # sensitive body capture was explicitly enabled.
    diagnostics.capture("full-comparison", "response", result.model_dump(mode="json"))
    return report


def _save_report(path: Path, report: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    import os
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tu", required=True, type=Path, help="PDF/DOCX TU file")
    parser.add_argument("--znp", required=True, type=Path, help="PDF/DOCX ZnP file")
    parser.add_argument("--out", type=Path, default=Path("diagnostics"))
    parser.add_argument("--dry-run", action="store_true", help="Parsing only; no paid API calls")
    parser.add_argument("--probe", action="store_true", help="One minimal real API request first")
    parser.add_argument("--no-embeddings", action="store_true")
    parser.add_argument("--max-calls", type=int, default=None)
    parser.add_argument("--targeted-calls", type=int, default=None)
    parser.add_argument(
        "--capture-payloads", action="store_true",
        help="Save source text, DeepSeek prompts and responses in PRIVATE LOCAL files",
    )
    args = parser.parse_args(argv)
    if args.max_calls is not None and args.max_calls < 1:
        parser.error("--max-calls must be >= 1")
    if args.targeted_calls is not None and args.targeted_calls < 0:
        parser.error("--targeted-calls must be >= 0")

    with diagnostics.diagnostic_run(
        args.out, capture_payloads=args.capture_payloads, strict=True,
    ):
        output = Path(diagnostics.trace_directory())
        try:
            report = run_pair(
                args.tu, args.znp, dry_run=args.dry_run,
                no_embeddings=args.no_embeddings,
                max_calls=args.max_calls, targeted_calls=args.targeted_calls,
                probe=args.probe,
            )
        except BaseException as exc:
            report = {
                "error_type": type(exc).__name__,
                "diagnostic": "Inspect events.jsonl and private payloads, if enabled.",
            }
            _save_report(output / "report.json", report)
            print(json.dumps({"trace_dir": str(output), **report}, ensure_ascii=False))
            raise
        _save_report(output / "report.json", report)
        print(json.dumps({"trace_dir": str(output), **report}, ensure_ascii=False))
        return 2 if report.get("degraded") or report.get("model_failure_kind") else 0


if __name__ == "__main__":
    sys.exit(main())
