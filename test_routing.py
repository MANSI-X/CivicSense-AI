import sqlite3
from pathlib import Path

from backend import routes


def setup_temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "DATABASE_DIR", tmp_path)
    monkeypatch.setattr(routes, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(routes, "UPLOAD_DIR", tmp_path / "uploads")
    routes.init_db()


def test_seeds_six_departments_and_other_fallback(tmp_path, monkeypatch):
    setup_temp_db(tmp_path, monkeypatch)
    with routes.connect_db() as conn:
        rows = conn.execute("SELECT name FROM departments").fetchall()
        names = {r["name"] for r in rows}
        assert names == set(routes.DEPARTMENTS)
        dept_id, reason = routes.route_department(conn, "unrecognized category")
        other_id = conn.execute(
            "SELECT id FROM departments WHERE name='Other Department'"
        ).fetchone()["id"]
        assert dept_id == other_id
        assert "Fallback" in reason


def test_known_category_routes_to_road(tmp_path, monkeypatch):
    setup_temp_db(tmp_path, monkeypatch)
    with routes.connect_db() as conn:
        dept_id, reason = routes.route_department(conn, "Road / Pothole")
        name = conn.execute("SELECT name FROM departments WHERE id=?", (dept_id,)).fetchone()["name"]
        assert name == "Road Department"
        assert reason == "Configured category mapping"


def test_unknown_category_does_not_fail(tmp_path, monkeypatch):
    setup_temp_db(tmp_path, monkeypatch)
    with routes.connect_db() as conn:
        dept_id, reason = routes.route_department(conn, "Something never mapped")
        name = conn.execute("SELECT name FROM departments WHERE id=?", (dept_id,)).fetchone()["name"]
        assert name == "Other Department"
        assert reason == "Fallback: category mapping missing"


def test_migration_preserves_legacy_complaint(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "DATABASE_DIR", tmp_path)
    monkeypatch.setattr(routes, "DATABASE_PATH", tmp_path / "legacy.db")
    monkeypatch.setattr(routes, "UPLOAD_DIR", tmp_path / "uploads")
    tmp_path.mkdir(exist_ok=True)
    with sqlite3.connect(routes.DATABASE_PATH) as conn:
        conn.execute("""CREATE TABLE complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT, complaint_id TEXT UNIQUE NOT NULL,
            name TEXT, category TEXT NOT NULL, description TEXT NOT NULL, location TEXT NOT NULL,
            image_filename TEXT, status TEXT NOT NULL DEFAULT 'Submitted', created_at TEXT NOT NULL,
            resolution_notes TEXT, updated_at TEXT
        )""")
        conn.execute("""INSERT INTO complaints(complaint_id,category,description,location,status,created_at,resolution_notes)
            VALUES('CIV-OLD1234','Road / Pothole','old pothole','Test Road','In Progress','2026-01-01T10:00:00','existing note')""")
        conn.commit()
    routes.init_db()
    with routes.connect_db() as conn:
        row = conn.execute("SELECT * FROM complaints WHERE complaint_id='CIV-OLD1234'").fetchone()
        assert row is not None
        assert row["resolution_notes"] == "existing note"
        assert row["department_id"] is not None
