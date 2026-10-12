import os
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import sessionmaker
from app.core.db import Base, get_db
from app.core import models  # noqa: F401
from app.main import app

@pytest.fixture
def client():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    def override():
        with Session() as db:
            yield db
    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    engine.dispose()
