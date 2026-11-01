"""Back up the existing SQLite DB, then apply additive CivicSense AI migrations."""
import shutil
from datetime import datetime
from pathlib import Path

from backend import routes

ROOT = Path(__file__).resolve().parent
db_path = ROOT / "database" / "civicsense.db"

if db_path.exists():
    backup = db_path.with_name(f"civicsense_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
    shutil.copy2(db_path, backup)
    print(f"Backup created: {backup}")
else:
    print("No existing database found; a new database will be initialized.")

routes.init_db()
print(f"Migration completed: {db_path}")

with routes.connect_db() as conn:
    print("Tables:")
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        print(" -", row["name"])
    print("Complaint count:", conn.execute("SELECT COUNT(*) FROM complaints").fetchone()[0])
    print("Departments:")
    for row in conn.execute("SELECT id, name, active FROM departments ORDER BY id"):
        print(f" - {row['id']}: {row['name']} (active={row['active']})")
