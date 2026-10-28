"""Verify migration of already uploaded files, not only fresh schema creation."""
from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run_alembic(database: Path, *args: str):
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+pysqlite:///{database}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


def test_upgrade_existing_document_and_backfill_chunks(tmp_path):
    database = tmp_path / "old_schema.sqlite3"

    run_alembic(database, "upgrade", "0001")
    raw = "An old engineering condition has 150 kW at the connection point. " * 70
    with sqlite3.connect(database) as conn:
        time = datetime.now(timezone.utc).isoformat()
        conn.execute("INSERT INTO projects (id, name, state, created_at) VALUES (?, ?, ?, ?)",
                     ("p", "old project", "draft", time))
        conn.execute("""INSERT INTO documents
                     (id, project_id, kind, filename, stored_path, mime_type, created_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     ("d", "p", "tu", "old.pdf", "/tmp/old.pdf", "application/pdf", time))
        conn.execute("INSERT INTO fragments (id, document_id, page, position, text) VALUES (?, ?, ?, ?, ?)",
                     ("f", "d", 45, 0, raw))
        conn.execute("INSERT INTO normatives (id, code, title, status) VALUES (?, ?, ?, ?)",
                     ("n", "ГОСТ 1.0-92", "old normative", "active"))

    run_alembic(database, "upgrade", "head")
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT extraction_version FROM documents WHERE id='d'").fetchone()[0] == "ocr-v1"
        assert conn.execute("SELECT block_type FROM fragments WHERE id='f'").fetchone()[0] == "page"
        rows = conn.execute("SELECT position, text, page_start, page_end FROM chunks WHERE document_id='d' ORDER BY position").fetchall()
        assert len(rows) > 1
        assert "".join(row[1] for row in rows) == raw
        assert all(row[2:] == (45, 45) for row in rows)
        assert conn.execute("SELECT COUNT(*) FROM chunk_sources").fetchone()[0] == len(rows)
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(normatives)")}
        assert columns["updated_at"][3] == 1
        assert conn.execute("SELECT updated_at FROM normatives WHERE id='n'").fetchone()[0]


def test_upgrade_0006_to_0007_adds_provenance_and_keeps_data(tmp_path):
    database = tmp_path / "requirements_0006.sqlite3"

    run_alembic(database, "upgrade", "0006")
    with sqlite3.connect(database) as conn:
        time = datetime.now(timezone.utc).isoformat()
        conn.execute("INSERT INTO projects (id, name, state, created_at) VALUES (?, ?, ?, ?)",
                     ("p", "existing project", "draft", time))
        conn.execute("""INSERT INTO documents
                     (id, project_id, kind, filename, stored_path, mime_type,
                      extraction_version, created_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                     ("d", "p", "tu", "old.pdf", "/tmp/old.pdf",
                      "application/pdf", "ocr-v1", time))
        conn.execute("""INSERT INTO fragments
                     (id, document_id, page, position, text, block_type,
                      extraction_method)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     ("f", "d", 1, 0, "1.1. Мощность 150 кВт", "paragraph", "text"))
        conn.execute("""INSERT INTO tu_requirements
                     (id, project_id, document_id, clause_number, category, text, created_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     ("r1", "p", "d", "1.1", "power", "Мощность 150 кВт", time))
        conn.execute("""INSERT INTO requirement_verifications
                     (id, project_id, requirement_id, status, is_confirmed_by_human,
                      created_at, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     ("v1", "p", "r1", "fulfilled", 0, time, time))

    run_alembic(database, "upgrade", "0007")
    with sqlite3.connect(database) as conn:
        tu_columns = {row[1] for row in conn.execute("PRAGMA table_info(tu_requirements)")}
        assert {"fragment_id", "page"} <= tu_columns
        verification_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(requirement_verifications)")
        }
        assert {"matched_znp_fragment_id", "matched_znp_page"} <= verification_columns

        assert conn.execute(
            "SELECT clause_number, text, fragment_id, page FROM tu_requirements WHERE id='r1'"
        ).fetchone() == ("1.1", "Мощность 150 кВт", None, None)
        assert conn.execute(
            "SELECT status, matched_znp_fragment_id, matched_znp_page "
            "FROM requirement_verifications WHERE id='v1'"
        ).fetchone() == ("fulfilled", None, None)
        assert conn.execute("SELECT text FROM fragments WHERE id='f'").fetchone()[0] == (
            "1.1. Мощность 150 кВт"
        )

    run_alembic(database, "downgrade", "0006")
    with sqlite3.connect(database) as conn:
        tu_columns = {row[1] for row in conn.execute("PRAGMA table_info(tu_requirements)")}
        assert "fragment_id" not in tu_columns and "page" not in tu_columns
        verification_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(requirement_verifications)")
        }
        assert "matched_znp_fragment_id" not in verification_columns
        assert "matched_znp_page" not in verification_columns
        assert conn.execute(
            "SELECT clause_number, text FROM tu_requirements WHERE id='r1'"
        ).fetchone() == ("1.1", "Мощность 150 кВт")
        assert conn.execute(
            "SELECT status FROM requirement_verifications WHERE id='v1'"
        ).fetchone()[0] == "fulfilled"
