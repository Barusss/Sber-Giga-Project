from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.documents.router import router as documents_router
from app.normatives.router import router as normatives_router
from app.requirements.router import router as requirements_router
from app.reports.router import router as reports_router
from app.search.router import router as search_router

app = FastAPI(title="Sber Giga Project", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=[item.strip() for item in settings.cors_origins.split(",")], allow_methods=["*"], allow_headers=["*"])

@app.get("/health")
def health():
    return {"status": "ok"}

for router in (documents_router, normatives_router, requirements_router, reports_router, search_router):
    app.include_router(router, prefix="/api/v1")
