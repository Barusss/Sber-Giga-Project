from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/requirements", tags=["requirements"])

@router.post("/analyze", status_code=501)
def analyze_requirements():
    raise HTTPException(501, "Semantic requirement matching will be implemented by participant 3")
