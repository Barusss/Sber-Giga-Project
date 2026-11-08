"""Read persisted comparisons for an explicitly selected document pair."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.models import Document, RequirementAnalysis
from app.core.schemas import DocumentOut
from app.requirements.router import get_project_requirements


def build_requirements_report(db: Session, project_id: str, tu_document_id: str | None, znp_document_id: str | None) -> dict:
    documents = db.scalars(select(Document).where(Document.project_id == project_id)).all()
    by_id = {document.id: document for document in documents}
    runs = db.scalars(select(RequirementAnalysis).where(
        RequirementAnalysis.tu_document_id.in_(by_id),
        RequirementAnalysis.znp_document_id.in_(by_id),
    ).order_by(RequirementAnalysis.completed_at.desc())).all()
    pairs = [{"tu_document_id": run.tu_document_id, "znp_document_id": run.znp_document_id,
        "tu_filename": by_id[run.tu_document_id].filename, "znp_filename": by_id[run.znp_document_id].filename,
        "completed_at": run.completed_at.isoformat()} for run in runs]
    if tu_document_id is None and znp_document_id is None and len(runs) == 1:
        tu_document_id, znp_document_id = runs[0].tu_document_id, runs[0].znp_document_id
    result = {"available_pairs": pairs, "pair": None, "complete": False, "stats": None, "confirmed": 0, "items": []}
    if tu_document_id is None or znp_document_id is None:
        return result
    run = next((r for r in runs if r.tu_document_id == tu_document_id and r.znp_document_id == znp_document_id), None)
    if run is None:
        raise ValueError("Сохранённая проверка выбранной пары не найдена")
    pair = next(p for p in pairs if p["tu_document_id"] == tu_document_id and p["znp_document_id"] == znp_document_id)
    response = get_project_requirements(project_id, tu_document_id, znp_document_id, db)
    entries = [item.model_dump(mode="json") for item in response.checked_requirements]
    tu_pagination = DocumentOut.model_validate(by_id[tu_document_id]).pagination_kind
    znp_pagination = DocumentOut.model_validate(by_id[znp_document_id]).pagination_kind
    for item in entries:
        if item.get("validation_details") is None:
            item["historical_reasoning"] = item.get("reasoning")
            item["reasoning"] = "Результат прежнего формата; подробности автоматической проверки не сохранены."
        if tu_pagination != "physical":
            item["tu_page"] = None
            item["requirement"]["page"] = None
        if znp_pagination != "physical":
            item["znp_page"] = None
            if item["matched_clause"]:
                item["matched_clause"]["page"] = None
            for part in (item.get("validation_details") or {}).get("items", []):
                for evidence in part.get("evidence", []):
                    evidence["logical_page"] = evidence.get("page")
                    evidence["page"] = None
                    evidence["pagination_kind"] = znp_pagination
    result.update({"pair": pair, "complete": bool(run.requirement_ids) and set(run.requirement_ids) == {item["requirement"]["id"] for item in entries},
        "stats": response.stats, "confirmed": sum(item["is_confirmed_by_human"] for item in entries), "items": entries})
    return result
