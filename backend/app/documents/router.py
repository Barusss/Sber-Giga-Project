from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
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

@router.post("/projects/{project_id}/documents", response_model=DocumentOut, status_code=201)
async def upload_document(project_id: str, kind: DocumentKind = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)):
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
    try:
        blocks = extract_blocks(data, suffix)
    except Exception as exc:
        raise HTTPException(422, "Cannot parse this document") from exc
    target = settings.upload_dir / f"{uuid4().hex}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    document = Document(project_id=project_id, kind=kind, filename=Path(file.filename or "document").name[:255], stored_path=str(target), mime_type=file.content_type or "application/octet-stream")
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

@router.get("/projects/{project_id}/documents", response_model=list[DocumentOut])
def list_documents(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return db.scalars(select(Document).where(Document.project_id == project_id)).all()

@router.get("/documents/{document_id}/fragments", response_model=list[FragmentOut])
def get_fragments(document_id: str, db: Session = Depends(get_db)):
    if not db.get(Document, document_id):
        raise HTTPException(404, "Document not found")
    return db.scalars(select(Fragment).where(Fragment.document_id == document_id).order_by(Fragment.page, Fragment.position)).all()
