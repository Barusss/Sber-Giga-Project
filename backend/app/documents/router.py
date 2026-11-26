import json
import logging
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.db import get_db
from app.core.models import Chunk, ChunkSource, Document, DocumentKind, Fragment, Project, ReviewState
from app.core.schemas import DocumentOut, FragmentOut, ProjectCreate, ProjectOut
from app.documents.parser import extract_blocks, pdf_layout_version
from app.documents.chunking import build_chunks, CHUNKING_VERSION
from app.documents.storage import require_project, read_upload, save_document, write_chunk_index

router = APIRouter(tags=["documents"])

@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(data: ProjectCreate, db: Session = Depends(get_db)):
    project = Project(name=data.name)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project

@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    return db.scalars(select(Project).order_by(Project.created_at.desc())).all()

@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, db: Session = Depends(get_db)):
    return require_project(db, project_id)

logger = logging.getLogger(__name__)


@router.post("/projects/{project_id}/documents", response_model=DocumentOut, status_code=201)
async def upload_document(project_id: str, kind: DocumentKind = Form(...),
                          file: UploadFile = File(...), db: Session = Depends(get_db)):
    data, suffix = await read_upload(project_id, file, db)
    try:
        blocks = await run_in_threadpool(extract_blocks, data, suffix)
    except Exception as exc:
        logger.exception("Document parsing failed")
        raise HTTPException(422, "Cannot parse this document") from exc
    return save_document(project_id, kind, file, data, suffix, blocks, db)


def _upload_event(stage: str, message: str, **details) -> str:
    # NDJSON is suitable for an uploaded POST: no second polling request or worker state.
    return json.dumps({"stage": stage, "message": message, **details}, ensure_ascii=False) + "\n"


@router.post("/projects/{project_id}/documents/stream")
async def upload_document_stream(project_id: str, kind: DocumentKind = Form(...),
                                 file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Opt-in progress stream for the UI. The regular upload contract is unchanged."""
    data, suffix = await read_upload(project_id, file, db)

    trace_id = uuid4().hex[:12]

    def report(stage: str, message: str, **details) -> str:
        return _upload_event(stage, message, trace_id=trace_id, **details)

    def process():
        yield report("received", "Файл получен сервером")
        yield report("parsing", "Извлечение текста и OCR сканированных страниц")
        try:
            blocks = extract_blocks(data, suffix)
        except Exception:
            logger.exception("Document parsing failed, trace_id=%s", trace_id)
            yield report("error", "Не удалось распознать документ", step="parsing")
            return

        pages = len({block.page for block in blocks})
        yield report("parsed", f"Извлечено фрагментов: {len(blocks)}", pages=pages, fragments=len(blocks))
        yield report("indexing", "Сохранение текста и построение поискового индекса")
        try:
            document = save_document(project_id, kind, file, data, suffix, blocks, db)
        except Exception:
            logger.exception("Document saving failed, trace_id=%s", trace_id)
            yield report("error", "Не удалось сохранить документ", step="indexing")
            return
        yield report("complete", "Документ успешно обработан",
                            document=DocumentOut.model_validate(document).model_dump(mode="json"))

    return StreamingResponse(process(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})



@router.post("/documents/{document_id}/reindex-chunks")
def reindex_document_chunks(document_id: str, db: Session = Depends(get_db)):
    """Rebuild chunks without replacing fragments or losing cited findings.

    The original file must still match all existing fragments. Only section
    metadata and the derived chunk index change; fragment IDs stay the same.
    Previous chunk embeddings are invalidated and must be regenerated.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    project = require_project(db, document.project_id)
    if project.state == ReviewState.approved:
        raise HTTPException(409, "Approved project cannot be modified")

    path = Path(document.stored_path)
    if not path.is_file():
        raise HTTPException(409, "Original document file is unavailable")
    try:
        blocks = extract_blocks(path.read_bytes(), path.suffix.lower())
    except Exception as exc:
        logger.exception("Document reindex parsing failed")
        raise HTTPException(422, "Cannot parse the stored document") from exc

    fragments = db.scalars(
        select(Fragment).where(Fragment.document_id == document_id)
        .order_by(Fragment.page, Fragment.position)
    ).all()
    if len(fragments) != len(blocks) or any(
        (fragment.page, fragment.position, fragment.text)
        != (block.page, block.position, block.text)
        for fragment, block in zip(fragments, blocks)
    ):
        raise HTTPException(
            409, "Source fragments changed after parsing (old scans must be uploaded again to preserve citation IDs)",
        )

    for fragment, block in zip(fragments, blocks):
        fragment.block_type = block.block_type
        fragment.section_path = block.section_path
        fragment.container_id = block.container_id
    if path.suffix.lower() == ".pdf":
        document.extraction_version = pdf_layout_version(document.extraction_version.split("+")[0], blocks)

    chunk_ids = select(Chunk.id).where(Chunk.document_id == document_id)
    db.execute(delete(ChunkSource).where(ChunkSource.chunk_id.in_(chunk_ids)))
    db.execute(delete(Chunk).where(Chunk.document_id == document_id))
    db.flush()

    drafts = build_chunks(fragments)
    write_chunk_index(db, document_id, drafts)

    db.commit()
    return {
        "document_id": document_id,
        "fragments_updated": len(fragments),
        "chunks": len(drafts),
        "chunking_version": CHUNKING_VERSION,
    }


@router.get("/projects/{project_id}/documents", response_model=list[DocumentOut])
def list_documents(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return db.scalars(select(Document).where(Document.project_id == project_id)).all()

@router.get("/documents/{document_id}/fragments", response_model=list[FragmentOut])
def get_fragments(document_id: str, db: Session = Depends(get_db)):
    if not db.get(Document, document_id):
        raise HTTPException(404, "Document not found")
    return db.scalars(select(Fragment).where(Fragment.document_id == document_id).order_by(Fragment.page, Fragment.position)).all()
