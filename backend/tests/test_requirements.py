import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select, StaticPool
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.models import (
    Document,
    DocumentKind,
    Project,
    RequirementCategory,
    RequirementStatus,
    RequirementVerification,
    TURequirement,
)
from app.requirements.schemas import (
    CheckRequirementsRequest,
    CheckRequirementsResponse,
    ConfirmRequirementRequest,
    LLMVerificationDecision,
    RequirementCheckItem,
    RequirementCategory as SchemaCategory,
    RequirementStatus as SchemaStatus,
    TURequirement as SchemaTURequirement,
    ZNPClause as SchemaZNPClause,
)


@pytest.fixture
def db_session():
    """Изолированная сессия SQLite в памяти для проверки моделей."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        yield session
    engine.dispose()


# ============================================================================
# 1. Валидация схем Pydantic
# ============================================================================

def test_tu_requirement_schema_valid():
    req = SchemaTURequirement(
        id="req-1",
        clause_number="п. 4.1",
        category=SchemaCategory.power,
        text="Максимальная мощность 150 кВт",
    )
    assert req.id == "req-1"
    assert req.category == SchemaCategory.power
    assert req.clause_number == "п. 4.1"


def test_tu_requirement_schema_invalid_category():
    with pytest.raises(ValidationError):
        SchemaTURequirement(
            id="req-1",
            clause_number="п. 4.1",
            category="invalid_category",  # type: ignore
            text="Текст требования",
        )


def test_llm_verification_decision_schema():
    decision = LLMVerificationDecision(
        status=SchemaStatus.fulfilled,
        similarity_score=0.92,
        matched_znp_clause_id="znp-12",
        tu_quote="150 кВт",
        znp_quote="Предусмотреть мощность 150 кВт",
        reasoning="Параметр мощности полностью совпадает.",
    )
    assert decision.status == SchemaStatus.fulfilled
    assert decision.similarity_score == 0.92
    assert decision.matched_znp_clause_id == "znp-12"


def test_requirement_check_item_hitl_defaults():
    tu = SchemaTURequirement(
        id="req-1",
        clause_number="п. 1",
        category=SchemaCategory.voltage,
        text="Класс напряжения 10 кВ",
    )
    item = RequirementCheckItem(
        id="check-1",
        requirement=tu,
        status=SchemaStatus.needs_review,
        confidence_score=0.65,
    )
    assert item.is_confirmed_by_human is False
    assert item.human_comment is None
    assert item.human_override_status is None


def test_check_requirements_response_stats():
    resp = CheckRequirementsResponse(
        checked_requirements=[],
        stats={"fulfilled": 10, "missing": 2, "needs_review": 1},
        trace_info={"execution_time_ms": 450, "model": "deepseek-flash"},
    )
    data = resp.model_dump()
    assert data["stats"]["fulfilled"] == 10
    assert data["trace_info"]["model"] == "deepseek-flash"


def test_confirm_requirement_request():
    req = ConfirmRequirementRequest(
        requirement_id="req-1",
        status=SchemaStatus.fulfilled,
        comment="Согласовано с главным энергетиком",
    )
    assert req.status == SchemaStatus.fulfilled
    assert req.comment == "Согласовано с главным энергетиком"


# ============================================================================
# 2. Модели базы данных (SQLAlchemy ORM)
# ============================================================================

def test_db_create_and_query_tu_requirement(db_session):
    project = Project(name="Тестовый проект ТУ")
    db_session.add(project)
    db_session.flush()

    doc = Document(
        project_id=project.id,
        kind=DocumentKind.tu,
        filename="tu_sample.pdf",
        stored_path="/data/tu.pdf",
        mime_type="application/pdf",
    )
    db_session.add(doc)
    db_session.flush()

    requirement = TURequirement(
        project_id=project.id,
        document_id=doc.id,
        clause_number="п. 3.2.1",
        category=RequirementCategory.metering,
        text="Установить прибор учета класса точности 0.5S",
    )
    db_session.add(requirement)
    db_session.commit()

    saved = db_session.scalar(
        select(TURequirement).where(TURequirement.id == requirement.id)
    )
    assert saved is not None
    assert saved.clause_number == "п. 3.2.1"
    assert saved.category == RequirementCategory.metering
    assert saved.project_id == project.id
    assert saved.document_id == doc.id


def test_db_requirement_verification_hitl_workflow(db_session):
    project = Project(name="Проект верификации")
    db_session.add(project)
    db_session.flush()

    doc = Document(
        project_id=project.id,
        kind=DocumentKind.tu,
        filename="tu.pdf",
        stored_path="/data/tu.pdf",
        mime_type="application/pdf",
    )
    db_session.add(doc)
    db_session.flush()

    req = TURequirement(
        project_id=project.id,
        document_id=doc.id,
        clause_number="п. 5.1",
        category=RequirementCategory.protection,
        text="Установить микропроцессорные защиты присоединения",
    )
    db_session.add(req)
    db_session.flush()

    # 1. Первичное сохранение решения ИИ
    verification = RequirementVerification(
        project_id=project.id,
        requirement_id=req.id,
        status=RequirementStatus.needs_review,
        confidence_score=0.72,
        matched_znp_clause_number="п. 8.4",
        matched_znp_text="Релейную защиту выполнить согласно ПУЭ",
        tu_quote="микропроцессорные защиты",
        znp_quote="согласно ПУЭ",
        reasoning="Общая формулировка без детализации типа терминала",
        is_confirmed_by_human=False,
    )
    db_session.add(verification)
    db_session.commit()

    saved_ver = db_session.scalar(
        select(RequirementVerification).where(RequirementVerification.requirement_id == req.id)
    )
    assert saved_ver is not None
    assert saved_ver.is_confirmed_by_human is False

    # 2. Имитация ручного подтверждения инженером
    saved_ver.is_confirmed_by_human = True
    saved_ver.human_override_status = RequirementStatus.fulfilled
    saved_ver.human_comment = "Тип терминала согласован письмом"
    db_session.commit()

    # 3. Проверка обновленного состояния
    refreshed = db_session.scalar(
        select(RequirementVerification).where(RequirementVerification.id == saved_ver.id)
    )
    assert refreshed.is_confirmed_by_human is True
    assert refreshed.human_override_status == RequirementStatus.fulfilled
    assert refreshed.human_comment == "Тип терминала согласован письмом"

def test_requirements_endpoint_validation(client):
    """Пустой запрос к реализованному эндпоинту анализа должен возвращать 422."""
    response = client.post("/api/v1/requirements/analyze")
    assert response.status_code == 422


def test_analyze_requirements_agent_pipeline(client):
    """Проверка полного цикла работы агента сверки ТУ и ЗнП."""
    payload = {
        "tu_requirements": [
            {
                "id": "tu-1",
                "clause_number": "п. 1.1",
                "category": "power",
                "text": "Максимальная мощность присоединяемых устройств составляет 150 кВт.",
            },
            {
                "id": "tu-2",
                "clause_number": "п. 2.1",
                "category": "metering",
                "text": "Установить трехфазный счетчик электрической энергии класса точности 0.5S.",
            },
        ],
        "znp_clauses": [
            {
                "id": "znp-1",
                "clause_number": "Раздел 3.1",
                "text": "Проектом предусмотреть максимальную мощность энергопринимающих устройств 150 кВт.",
            }
        ],
    }
    response = client.post("/api/v1/requirements/analyze", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["stats"]["total"] == 2
    assert data["stats"]["fulfilled"] == 1
    assert data["stats"]["missing"] == 1
    assert "agent_runs" in data["trace_info"]