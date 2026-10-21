import json
import logging
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.db import get_db
from app.core.models import Chunk, ChunkSource, Document, DocumentKind, Fragment, Project, ReviewState
from app.core.schemas import DocumentOut, FragmentOut, ProjectCreate, ProjectOut
from app.documents.parser import extract_blocks
from app.documents.chunking import build_chunks, CHUNKING_VERSION

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

def require_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project

logger = logging.getLogger(__name__)


async def _read_upload(project_id: str, file: UploadFile, db: Session):
    project = require_project(db, project_id)
    if project.state == ReviewState.approved:
        raise HTTPException(409, "Approved project cannot be modified")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise HTTPException(415, "Supported formats: PDF, DOCX")
    size_limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(size_limit + 1)
    if len(data) > size_limit:
        raise HTTPException(413, "File too large")
    if not data:
        raise HTTPException(422, "Empty file")
    return data, suffix


def _save_document(project_id: str, kind: DocumentKind, file: UploadFile,
                   data: bytes, suffix: str, blocks: list, db: Session) -> Document:
    target = settings.upload_dir / f"{uuid4().hex}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    try:
        document = Document(
            project_id=project_id, kind=kind,
            filename=Path(file.filename or "document").name[:255],
            stored_path=str(target), mime_type=file.content_type or "application/octet-stream"
        )
        db.add(document)
        db.flush()
        fragments = []
        for block in blocks:
            fragment = Fragment(document_id=document.id, **vars(block))
            db.add(fragment)
            fragments.append(fragment)
        db.flush()
        for position, draft in enumerate(build_chunks(fragments)):
            chunk = Chunk(document_id=document.id, position=position, text=draft.text,
                          page_start=draft.page_start, page_end=draft.page_end,
                          section_path=draft.section_path, chunking_version=CHUNKING_VERSION)
            db.add(chunk)
            db.flush()
            for source_position, piece in enumerate(draft.pieces):
                db.add(ChunkSource(chunk_id=chunk.id, fragment_id=piece.fragment.id,
                                   position=source_position, start_offset=piece.start, end_offset=piece.end))
        db.commit()
        db.refresh(document)
        return document
    except Exception:
        db.rollback()
        target.unlink(missing_ok=True)
        raise


@router.post("/projects/{project_id}/documents", response_model=DocumentOut, status_code=201)
async def upload_document(project_id: str, kind: DocumentKind = Form(...),
                          file: UploadFile = File(...), db: Session = Depends(get_db)):
    data, suffix = await _read_upload(project_id, file, db)
    try:
        blocks = extract_blocks(data, suffix)
    except Exception as exc:
        logger.exception("Document parsing failed")
        raise HTTPException(422, "Cannot parse this document") from exc
    return _save_document(project_id, kind, file, data, suffix, blocks, db)


def _upload_event(stage: str, message: str, **details) -> str:
    # NDJSON is suitable for an uploaded POST: no second polling request or worker state.
    return json.dumps({"stage": stage, "message": message, **details}, ensure_ascii=False) + "\n"


@router.post("/projects/{project_id}/documents/stream")
async def upload_document_stream(project_id: str, kind: DocumentKind = Form(...),
                                 file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Opt-in progress stream for the UI. The regular upload contract is unchanged."""
    data, suffix = await _read_upload(project_id, file, db)

    def process():
        yield _upload_event("received", "Файл получен сервером")
        yield _upload_event("parsing", "Извлечение текста и OCR сканированных страниц")
        try:
            blocks = extract_blocks(data, suffix)
        except Exception:
            logger.exception("Document parsing failed during streamed upload")
            yield _upload_event("error", "Не удалось распознать документ", step="parsing")
            return

        pages = len({block.page for block in blocks})
        yield _upload_event("parsed", f"Извлечено фрагментов: {len(blocks)}", pages=pages, fragments=len(blocks))
        yield _upload_event("indexing", "Сохранение текста и построение поискового индекса")
        try:
            document = _save_document(project_id, kind, file, data, suffix, blocks, db)
        except Exception:
            logger.exception("Document saving failed during streamed upload")
            yield _upload_event("error", "Не удалось сохранить документ", step="indexing")
            return
        yield _upload_event("complete", "Документ успешно обработан",
                            document=DocumentOut.model_validate(document).model_dump(mode="json"))

    return StreamingResponse(process(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/projects/{project_id}/documents", response_model=list[DocumentOut])
def list_documents(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return db.scalars(select(Document).where(Document.project_id == project_id)).all()

@router.get("/documents/{document_id}/fragments", response_model=list[FragmentOut])
def get_fragments(document_id: str, db: Session = Depends(get_db)):
    if not db.get(Document, document_id):
        raise HTTPException(404, "Document not found")
    return db.scalars(select(Fragment).where(Fragment.document_id == document_id).order_by(Fragment.page, Fragment.position)).all()
