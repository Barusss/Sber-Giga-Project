"""Document validation and persistence shared by HTTP and background operations."""
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.models import Chunk, ChunkSource, Document, DocumentKind, Fragment, Project, ReviewState
from app.documents.chunking import build_chunks, CHUNKING_VERSION
from app.documents.parser import pdf_layout_version


def require_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


async def read_upload(project_id: str, file: UploadFile, db: Session):
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


def save_document(project_id: str, kind: DocumentKind, file: UploadFile,
                   data: bytes, suffix: str, blocks: list, db: Session) -> Document:
    if not build_chunks(blocks):
        raise HTTPException(422, "No useful text extracted; check the document and OCR settings")
    target = settings.upload_dir / f"{uuid4().hex}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    try:
        version = "text-v3"
        if any(b.extraction_method == "ocr" for b in blocks):
            version = "paddleocr-v5" if settings.ocr_backend == "paddleocr" else "tesseract-v3"
        if suffix == ".pdf":
            version = pdf_layout_version(version, blocks)
        document = Document(
            project_id=project_id, kind=kind,
            extraction_version=version,
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
        write_chunk_index(db, document.id, build_chunks(fragments))
        db.commit()
        db.refresh(document)
        return document
    except Exception:
        db.rollback()
        target.unlink(missing_ok=True)
        raise


def write_chunk_index(db: Session, document_id: str, drafts) -> None:
    for position, draft in enumerate(drafts):
        chunk = Chunk(
            document_id=document_id,
            position=position,
            text=draft.text,
            page_start=draft.page_start,
            page_end=draft.page_end,
            section_path=draft.section_path,
            chunking_version=CHUNKING_VERSION,
        )
        db.add(chunk)
        db.flush()
        for source_position, piece in enumerate(draft.pieces):
            db.add(ChunkSource(
                chunk_id=chunk.id, fragment_id=piece.fragment.id,
                position=source_position,
                start_offset=piece.start, end_offset=piece.end,
            ))
