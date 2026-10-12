from enum import Enum
from typing import List, Optional
from pydantic import BaseModel


class RequirementStatus(str, Enum):
    fulfilled = "fulfilled"
    missing = "missing"
    needs_review = "needs_review"


class RequirementCategory(str, Enum):
    power = "power"
    reliability = "reliability"
    voltage = "voltage"
    connection_point = "connection_point"
    metering = "metering"
    protection = "protection"
    equipment = "equipment"
    other = "other"


class TURequirement(BaseModel):
    id: str
    clause_number: str
    category: RequirementCategory
    text: str


class ZNPClause(BaseModel):
    id: str
    clause_number: str
    text: str


class LLMVerificationDecision(BaseModel):
    status: RequirementStatus
    similarity_score: float
    matched_znp_clause_id: Optional[str] = None
    tu_quote: Optional[str] = None
    znp_quote: Optional[str] = None
    reasoning: Optional[str] = None


class RequirementCheckItem(BaseModel):
    id: str
    requirement: TURequirement
    status: RequirementStatus
    confidence_score: float
    matched_clause: Optional[ZNPClause] = None
    tu_quote: Optional[str] = None
    znp_quote: Optional[str] = None
    reasoning: Optional[str] = None
    is_confirmed_by_human: bool = False
    human_comment: Optional[str] = None
    human_override_status: Optional[RequirementStatus] = None


class CheckRequirementsRequest(BaseModel):
    tu_requirements: List[TURequirement]
    znp_clauses: List[ZNPClause]


class CheckRequirementsResponse(BaseModel):
    checked_requirements: List[RequirementCheckItem]
    stats: dict  # Например: {"fulfilled": 5, "missing": 2, "needs_review": 3}
    trace_info: Optional[dict] = None


class ConfirmRequirementRequest(BaseModel):
    requirement_id: str
    status: RequirementStatus
    comment: Optional[str] = None