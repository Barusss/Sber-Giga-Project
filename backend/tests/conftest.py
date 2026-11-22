import os
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.core.db import Base, get_db
from app.core import models  # noqa: F401
from app.main import app

# A developer's root .env may hold a real paid API key. Tests must never call
# external services; DeepSeek tests pass their own Settings explicitly.
settings.deepseek_api_key = SecretStr("")
settings.embedding_enabled = False

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

@pytest.fixture
def storage_matching_stub(monkeypatch):
    """Deterministic model for storage tests; never enables provider fallback."""
    import json
    from app.requirements.coverage import scope_overlap
    def reply(self, *, system_prompt, user_prompt):
        p = json.loads(user_prompt)
        if 'znp_atom' in p:
            atom = p['znp_atom']
            quote = (atom.get('source_fragments') or [{'text': atom['text']}])[0]['text']
            return {'links': [dict(requirement_id=c['requirement_id'], relation='supports',
                tu_quote=(c.get('source_fragments') or [{'text': c['text']}])[0]['text'],
                znp_quote=quote, confidence=.95, reasoning='Storage fixture')
                for c in p['tu_candidates'] if scope_overlap(c['text'], atom['text'])], 'search_more': False}
        if 'obligation' in p:
            return {'covered': False, 'object_consistent': True,
                    'evidence_ids': [e['link_id'] for e in p.get('evidence', [])]}
        return {'atoms': [], 'links': [], 'conflicts': [], 'uncertain_ids': []}
    monkeypatch.setattr('app.ai.deepseek.DeepSeekClient.complete_json', reply)
