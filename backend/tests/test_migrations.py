"""Verify migration of already uploaded files, not only fresh schema creation."""
from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3
import subprocess


def test_upgrade_existing_document_and_backfill_chunks(tmp_path):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "old_schema.sqlite3"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+pysqlite:///{database}"

    def upgrade(version):
        result = subprocess.run(
            ["alembic", "upgrade", version], cwd=root, env=env,
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr

    upgrade("0001")
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

    upgrade("head")
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
