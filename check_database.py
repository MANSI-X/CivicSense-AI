import sqlite3
from pathlib import Path

db_path = Path(__file__).resolve().parent / "database" / "civicsense.db"

if not db_path.exists():
    print("Database file not found:", db_path)
    raise SystemExit(1)

with sqlite3.connect(db_path) as conn:
    conn.row_factory = sqlite3.Row
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    print("Tables:", ", ".join(sorted(tables)))
    if "complaints" in tables:
        print("Total complaints:", conn.execute("SELECT COUNT(*) FROM complaints").fetchone()[0])
        print("Complaint columns:")
        for column in conn.execute("PRAGMA table_info(complaints)"):
            print("-", column["name"])
    if "departments" in tables:
        print("\nDepartments:")
        for d in conn.execute("SELECT id,name,active FROM departments ORDER BY id"):
            print(f"- {d['id']}: {d['name']} (active={d['active']})")
