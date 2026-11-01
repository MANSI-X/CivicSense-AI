import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, send_from_directory, session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

main_bp = Blueprint("main", __name__)
BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_DIR = BASE_DIR / "database"
DATABASE_PATH = DATABASE_DIR / "civicsense.db"
UPLOAD_DIR = BASE_DIR / "uploads" / "complaints"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
STATUSES = {"Submitted", "Pending", "Under Review", "Assigned", "In Progress", "Resolved", "Rejected"}
DEPARTMENTS = [
    "Electricity Department", "Municipal Corporation", "Road Department",
    "Water Department", "Sanitation Department", "Other Department"
]
DEFAULT_MAPPING = {
    "Road / Pothole": "Road Department",
    "Road Damage": "Road Department",
    "Garbage / Sanitation": "Sanitation Department",
    "Sanitation": "Sanitation Department",
    "Streetlight": "Municipal Corporation",
    "Water Leakage": "Water Department",
    "Water Supply": "Water Department",
    "Electricity": "Electricity Department",
    "Power Outage": "Electricity Department",
    "Electrical Fault": "Electricity Department",
    "Drainage / Sewage": "Other Department",
    "Other": "Other Department",
}
TRANSITIONS = {
    "Submitted": {"Pending", "Under Review", "Assigned", "Rejected"},
    "Pending": {"Under Review", "Assigned", "Rejected"},
    "Under Review": {"Pending", "Assigned", "In Progress", "Rejected"},
    "Assigned": {"In Progress", "Pending", "Rejected"},
    "In Progress": {"Resolved", "Assigned", "Rejected"},
    "Resolved": {"In Progress"},
    "Rejected": {"Pending", "Under Review"},
}


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect_db():
    conn = sqlite3.connect(DATABASE_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                complaint_id TEXT UNIQUE NOT NULL,
                name TEXT, category TEXT NOT NULL, description TEXT NOT NULL,
                location TEXT NOT NULL, image_filename TEXT,
                status TEXT NOT NULL DEFAULT 'Submitted', created_at TEXT NOT NULL,
                resolution_notes TEXT, updated_at TEXT,
                department_id INTEGER, assigned_worker_id INTEGER,
                routing_reason TEXT, resolution_proof_filename TEXT
            )
        """)
        # Additive migrations preserve existing complaint rows.
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(complaints)")}
        additions = {
            "resolution_notes": "TEXT", "updated_at": "TEXT",
            "department_id": "INTEGER", "assigned_worker_id": "INTEGER",
            "routing_reason": "TEXT", "resolution_proof_filename": "TEXT",
        }
        for name, kind in additions.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE complaints ADD COLUMN {name} {kind}")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS departments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('department_admin')),
                department_id INTEGER NOT NULL REFERENCES departments(id),
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS field_workers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, username TEXT NOT NULL UNIQUE,
                phone TEXT, department_id INTEGER NOT NULL REFERENCES departments(id),
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS category_mappings (
                category TEXT PRIMARY KEY,
                department_id INTEGER NOT NULL REFERENCES departments(id),
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS complaint_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                complaint_id INTEGER NOT NULL REFERENCES complaints(id) ON DELETE CASCADE,
                old_status TEXT, new_status TEXT NOT NULL, note TEXT,
                actor_username TEXT NOT NULL, created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                complaint_id INTEGER REFERENCES complaints(id) ON DELETE CASCADE,
                message TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        for name in DEPARTMENTS:
            conn.execute(
                "INSERT OR IGNORE INTO departments(name, active, created_at) VALUES (?, 1, ?)",
                (name, now_iso()),
            )
        ids = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM departments")}
        for category, dept_name in DEFAULT_MAPPING.items():
            conn.execute(
                "INSERT OR IGNORE INTO category_mappings(category, department_id, updated_at) VALUES (?,?,?)",
                (category, ids[dept_name], now_iso()),
            )
        # Route legacy and newly unassigned complaints. Unknowns go to Other Department.
        rows = conn.execute("""
            SELECT id, category FROM complaints WHERE department_id IS NULL
        """).fetchall()
        for row in rows:
            mapping = conn.execute("""
                SELECT d.id, d.name, d.active FROM category_mappings m
                JOIN departments d ON d.id=m.department_id WHERE m.category=?
            """, (row["category"],)).fetchone()
            reason = "Configured category mapping" if mapping else "Fallback: category mapping missing"
            if not mapping or not mapping["active"]:
                fallback = conn.execute(
                    "SELECT id FROM departments WHERE name='Other Department' AND active=1"
                ).fetchone()
                dept_id = fallback["id"] if fallback else ids["Other Department"]
                if mapping and not mapping["active"]:
                    reason = "Fallback: mapped department inactive"
            else:
                dept_id = mapping["id"]
            conn.execute(
                "UPDATE complaints SET department_id=?, routing_reason=? WHERE id=?",
                (dept_id, reason, row["id"]),
            )
            conn.execute("""
                INSERT INTO complaint_history
                    (complaint_id, old_status, new_status, note, actor_username, created_at)
                VALUES (?, NULL, ?, ?, 'system-migration', ?)
            """, (row["id"], row["category"] and "Submitted" or "Submitted",
                  "Initial routing: " + reason, now_iso()))
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS validate_complaint_department_insert
            BEFORE INSERT ON complaints
            WHEN NEW.department_id IS NOT NULL AND
                 NOT EXISTS (SELECT 1 FROM departments WHERE id=NEW.department_id)
            BEGIN SELECT RAISE(ABORT, 'invalid complaint department'); END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS validate_complaint_department_update
            BEFORE UPDATE OF department_id, assigned_worker_id ON complaints
            WHEN NEW.department_id IS NOT NULL AND
                 NOT EXISTS (SELECT 1 FROM departments WHERE id=NEW.department_id)
            BEGIN SELECT RAISE(ABORT, 'invalid complaint department'); END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS validate_worker_department_update
            BEFORE UPDATE OF department_id, assigned_worker_id ON complaints
            WHEN NEW.assigned_worker_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM field_workers w
                WHERE w.id=NEW.assigned_worker_id AND w.department_id=NEW.department_id
            )
            BEGIN SELECT RAISE(ABORT, 'worker must belong to complaint department'); END
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_complaints_department ON complaints(department_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_complaints_status ON complaints(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_complaint ON complaint_history(complaint_id, id)")
        conn.commit()


def allowed_file(filename):
    return bool(filename and "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS)


def valid_image_upload(file_storage):
    """Check both extension and common image signatures; reset stream before saving."""
    if not file_storage or not allowed_file(file_storage.filename):
        return False
    head = file_storage.stream.read(16)
    file_storage.stream.seek(0)
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    if ext in {"jpg", "jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == "webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return False


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def valid_csrf():
    supplied = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    return bool(expected) and secrets.compare_digest(supplied, expected)


def require_csrf():
    if not valid_csrf():
        abort(400, description="Invalid form token. Refresh the page and try again.")


def current_role():
    return session.get("role")


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_role() not in {"main_admin", "department_admin"}:
            return redirect(url_for("main.admin_login"))
        if current_role() == "department_admin":
            with connect_db() as conn:
                user = conn.execute(
                    "SELECT active FROM users WHERE id=?", (session.get("user_id"),)
                ).fetchone()
            if not user or not user["active"]:
                session.clear()
                return redirect(url_for("main.admin_login"))
        return view(*args, **kwargs)
    return wrapped


def main_admin_required(view):
    @wraps(view)
    @admin_required
    def wrapped(*args, **kwargs):
        if current_role() != "main_admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def actor_name():
    return session.get("username", "system")


def route_department(conn, category):
    row = conn.execute("""
        SELECT d.id, d.name, d.active
        FROM category_mappings m JOIN departments d ON d.id=m.department_id
        WHERE m.category=?
    """, (category,)).fetchone()
    if row and row["active"]:
        return row["id"], "Configured category mapping"
    fallback = conn.execute(
        "SELECT id, active FROM departments WHERE name='Other Department'"
    ).fetchone()
    if not fallback or not fallback["active"]:
        # Keep the fallback record usable even if someone previously deactivated it.
        conn.execute("UPDATE departments SET active=1 WHERE name='Other Department'")
        fallback = conn.execute("SELECT id FROM departments WHERE name='Other Department'").fetchone()
    reason = "Fallback: category mapping missing" if row is None else "Fallback: mapped department inactive"
    return fallback["id"], reason


def create_notification(conn, department_id, complaint_row_id, message):
    users = conn.execute(
        "SELECT id FROM users WHERE department_id=? AND active=1", (department_id,)
    ).fetchall()
    for user in users:
        conn.execute(
            "INSERT INTO notifications(user_id, complaint_id, message, created_at) VALUES (?,?,?,?)",
            (user["id"], complaint_row_id, message, now_iso()),
        )


@main_bp.route("/api/health")
def health():
    return jsonify({"success": True, "status": "running"})


@main_bp.route("/", methods=["GET"])
def home():
    init_db()
    with connect_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM complaints").fetchone()[0]
        recent = conn.execute("""
            SELECT complaint_id, category, location, status, created_at
            FROM complaints ORDER BY id DESC LIMIT 5
        """).fetchall()
    return render_template("index.html", total=total, recent=recent, csrf_token=csrf_token())


@main_bp.route("/complaints", methods=["POST"])
def register_complaint():
    require_csrf()
    name = request.form.get("name", "").strip()[:100]
    category = request.form.get("category", "").strip()[:100]
    description = request.form.get("description", "").strip()
    location = request.form.get("location", "").strip()
    if not category or not description or not location:
        flash("Category, description and location are required.", "error")
        return redirect(url_for("main.home"))
    if len(description) > 3000 or len(location) > 300:
        flash("Description or location is too long.", "error")
        return redirect(url_for("main.home"))
    image = request.files.get("photo")
    image_filename = None
    if image and image.filename:
        if not valid_image_upload(image):
            flash("Upload a valid PNG, JPG, JPEG, or WEBP image.", "error")
            return redirect(url_for("main.home"))
        image_filename = f"{uuid.uuid4().hex}.{image.filename.rsplit('.', 1)[1].lower()}"
        image.save(UPLOAD_DIR / secure_filename(image_filename))
    complaint_id = "CIV-" + uuid.uuid4().hex[:8].upper()
    created_at = now_iso()
    try:
        with connect_db() as conn:
            department_id, reason = route_department(conn, category)
            cur = conn.execute("""
                INSERT INTO complaints
                (complaint_id,name,category,description,location,image_filename,status,created_at,department_id,routing_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (complaint_id, name or None, category, description, location,
                  image_filename, "Submitted", created_at, department_id, reason))
            row_id = cur.lastrowid
            conn.execute("""
                INSERT INTO complaint_history(complaint_id,old_status,new_status,note,actor_username,created_at)
                VALUES (?,NULL,'Submitted',?,?,?)
            """, (row_id, "Initial complaint submitted; " + reason, "citizen", created_at))
            create_notification(conn, department_id, row_id, f"New complaint {complaint_id} assigned to your department.")
            conn.commit()
    except sqlite3.Error:
        current_app.logger.exception("Could not save complaint")
        if image_filename:
            (UPLOAD_DIR / image_filename).unlink(missing_ok=True)
        flash("Unable to save complaint. Please try again.", "error")
        return redirect(url_for("main.home"))
    flash(f"Complaint registered! Your ID is {complaint_id}", "success")
    return redirect(url_for("main.home"))


@main_bp.route("/api/complaints/<complaint_id>")
def track_complaint(complaint_id):
    with connect_db() as conn:
        complaint = conn.execute("""
            SELECT complaint_id, category, description, location, status, created_at,
                   updated_at, resolution_notes
            FROM complaints WHERE complaint_id=?
        """, (complaint_id.strip().upper(),)).fetchone()
        if not complaint:
            return jsonify({"success": False, "message": "Complaint not found."}), 404
        history = conn.execute("""
            SELECT new_status, note, created_at FROM complaint_history
            WHERE complaint_id=(SELECT id FROM complaints WHERE complaint_id=?)
            ORDER BY id
        """, (complaint_id.strip().upper(),)).fetchall()
    return jsonify({"success": True, "complaint": dict(complaint), "history": [dict(h) for h in history]})


@main_bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if current_role() in {"main_admin", "department_admin"}:
        return redirect(url_for("main.admin_dashboard"))
    error = None
    if request.method == "POST":
        require_csrf()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        main_username = os.getenv("ADMIN_USERNAME", "")
        main_hash = os.getenv("ADMIN_PASSWORD_HASH", "")
        authenticated = False
        role = None
        user_id = None
        department_id = None
        if main_username and main_hash and secrets.compare_digest(username, main_username):
            try:
                authenticated = check_password_hash(main_hash, password)
            except (ValueError, TypeError):
                authenticated = False
            if authenticated:
                role = "main_admin"
        if not authenticated:
            with connect_db() as conn:
                user = conn.execute("""
                    SELECT id, username, password_hash, department_id, active
                    FROM users WHERE username=?
                """, (username,)).fetchone()
            if user and user["active"]:
                try:
                    authenticated = check_password_hash(user["password_hash"], password)
                except (ValueError, TypeError):
                    authenticated = False
                if authenticated:
                    role, user_id, department_id = "department_admin", user["id"], user["department_id"]
        if authenticated:
            session.clear()
            session["role"] = role
            session["username"] = username
            if user_id:
                session["user_id"] = user_id
                session["department_id"] = department_id
            session["csrf_token"] = secrets.token_urlsafe(32)
            return redirect(url_for("main.admin_dashboard"))
        error = "Invalid username or password, or account inactive."
    return render_template("admin_login.html", csrf_token=csrf_token(), error=error)


@main_bp.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    require_csrf()
    session.clear()
    return redirect(url_for("main.admin_login"))


@main_bp.route("/admin")
@admin_required
def admin_dashboard():
    status_filter = request.args.get("status", "").strip()
    search = request.args.get("q", "").strip()[:100]
    role = current_role()
    dept_id = session.get("department_id")
    where = []
    params = []
    if role == "department_admin":
        where.append("c.department_id=?")
        params.append(dept_id)
    if status_filter in STATUSES:
        where.append("c.status=?")
        params.append(status_filter)
    if search:
        where.append("(c.complaint_id LIKE ? OR c.location LIKE ? OR c.category LIKE ? OR c.description LIKE ?)")
        pattern = f"%{search}%"
        params.extend([pattern] * 4)
    sql = """
        SELECT c.*, d.name AS department_name, w.name AS worker_name
        FROM complaints c LEFT JOIN departments d ON d.id=c.department_id
        LEFT JOIN field_workers w ON w.id=c.assigned_worker_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY c.id DESC LIMIT 200"
    with connect_db() as conn:
        complaints = conn.execute(sql, params).fetchall()
        stats_query = "SELECT status,COUNT(*) AS count FROM complaints"
        stat_params = []
        if role == "department_admin":
            stats_query += " WHERE department_id=?"
            stat_params.append(dept_id)
        stats_query += " GROUP BY status"
        stats = {r["status"]: r["count"] for r in conn.execute(stats_query, stat_params)}
        total_query = "SELECT COUNT(*) FROM complaints" + (" WHERE department_id=?" if role == "department_admin" else "")
        total = conn.execute(total_query, [dept_id] if role == "department_admin" else []).fetchone()[0]
        departments = conn.execute("SELECT id,name,active FROM departments ORDER BY name").fetchall() if role == "main_admin" else []
        department_admins = conn.execute("""
            SELECT u.id,u.username,u.active,d.name AS department_name
            FROM users u JOIN departments d ON d.id=u.department_id ORDER BY d.name,u.username
        """).fetchall() if role == "main_admin" else []
        if role == "department_admin":
            workers = conn.execute(
                "SELECT id,name,department_id FROM field_workers WHERE active=1 AND department_id=? ORDER BY name",
                (dept_id,)
            ).fetchall()
            worker_records = conn.execute(
                "SELECT id,name,username,phone,department_id,active FROM field_workers WHERE department_id=? ORDER BY name",
                (dept_id,)
            ).fetchall()
        else:
            workers = conn.execute("""
                SELECT w.id,w.name,w.department_id,d.name AS department_name
                FROM field_workers w JOIN departments d ON d.id=w.department_id
                WHERE w.active=1 ORDER BY d.name,w.name
            """).fetchall()
            worker_records = conn.execute("""
                SELECT w.id,w.name,w.username,w.phone,w.department_id,w.active,d.name AS department_name
                FROM field_workers w JOIN departments d ON d.id=w.department_id ORDER BY d.name,w.name
            """).fetchall()
        dept_stats = conn.execute("""
            SELECT d.name, COUNT(c.id) AS total,
            SUM(CASE WHEN c.status='Resolved' THEN 1 ELSE 0 END) AS resolved
            FROM departments d LEFT JOIN complaints c ON c.department_id=d.id
            GROUP BY d.id ORDER BY d.name
        """).fetchall() if role == "main_admin" else []
        notifications = []
        if role == "department_admin":
            notifications = conn.execute("""
                SELECT message,created_at FROM notifications
                WHERE user_id=? ORDER BY id DESC LIMIT 10
            """, (session.get("user_id"),)).fetchall()
    return render_template(
        "admin_dashboard.html", complaints=complaints, stats=stats, total=total,
        statuses=sorted(STATUSES), status_filter=status_filter, search=search,
        csrf_token=csrf_token(), role=role, username=session.get("username"),
        departments=departments, dept_stats=dept_stats, workers=workers,
        department_admins=department_admins, worker_records=worker_records,
        notifications=notifications,
    )


@main_bp.route("/admin/complaints/<int:complaint_row_id>/update", methods=["POST"])
@admin_required
def admin_update_complaint(complaint_row_id):
    require_csrf()
    new_status = request.form.get("status", "").strip()
    notes = request.form.get("resolution_notes", "").strip()[:2000]
    worker_id_raw = request.form.get("worker_id", "").strip()
    target_department_id = complaint_department_raw = request.form.get("department_id", "").strip()
    proof = request.files.get("resolution_proof")
    if new_status not in STATUSES:
        abort(400, description="Invalid complaint status.")
    proof_name = None
    if proof and proof.filename:
        if not valid_image_upload(proof):
            abort(400, description="Resolution proof must be a valid PNG, JPG, JPEG, or WEBP image.")
        proof_name = f"proof_{uuid.uuid4().hex}.{proof.filename.rsplit('.', 1)[1].lower()}"
        proof.save(UPLOAD_DIR / secure_filename(proof_name))
    with connect_db() as conn:
        complaint = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_row_id,)).fetchone()
        if not complaint:
            abort(404)
        if current_role() == "department_admin" and complaint["department_id"] != session.get("department_id"):
            abort(403)
        target_department_id = complaint["department_id"]
        if current_role() == "main_admin" and complaint_department_raw:
            if not complaint_department_raw.isdigit():
                abort(400, description="Invalid department.")
            target = conn.execute("SELECT id,active FROM departments WHERE id=?", (int(complaint_department_raw),)).fetchone()
            if not target or not target["active"]:
                abort(400, description="Target department must be active.")
            target_department_id = target["id"]
        if new_status != complaint["status"] and new_status not in TRANSITIONS.get(complaint["status"], set()):
            if proof_name: (UPLOAD_DIR / proof_name).unlink(missing_ok=True)
            abort(400, description=f"Invalid status transition: {complaint['status']} → {new_status}")
        worker_id = complaint["assigned_worker_id"]
        if target_department_id != complaint["department_id"]:
            worker_id = None
        if worker_id_raw:
            if not worker_id_raw.isdigit():
                abort(400, description="Invalid worker.")
            worker_id = int(worker_id_raw)
            worker = conn.execute("SELECT department_id,active FROM field_workers WHERE id=?", (worker_id,)).fetchone()
            if not worker or not worker["active"] or worker["department_id"] != target_department_id:
                abort(400, description="Worker must be active and belong to the complaint's department.")
            if current_role() == "department_admin" and worker["department_id"] != session.get("department_id"):
                abort(403)
            if new_status == "Submitted":
                new_status = "Assigned"
        elif new_status == "Assigned" and not worker_id:
            flash("Select a field worker before assigning this complaint.", "error")
            return redirect(url_for("main.admin_dashboard"))
        if new_status == "Resolved" and not notes:
            flash("Add resolution notes before marking a complaint Resolved.", "error")
            return redirect(url_for("main.admin_dashboard"))
        updated = now_iso()
        if target_department_id != complaint["department_id"]:
            create_notification(conn, target_department_id, complaint_row_id,
                                f"Complaint {complaint['complaint_id']} reassigned to your department.")
        conn.execute("""
            UPDATE complaints SET status=?, resolution_notes=?, updated_at=?,
                department_id=?, assigned_worker_id=?, resolution_proof_filename=COALESCE(?,resolution_proof_filename)
            WHERE id=?
        """, (new_status, notes or complaint["resolution_notes"], updated,
              target_department_id, worker_id, proof_name, complaint_row_id))
        if new_status != complaint["status"]:
            conn.execute("""
                INSERT INTO complaint_history(complaint_id,old_status,new_status,note,actor_username,created_at)
                VALUES (?,?,?,?,?,?)
            """, (complaint_row_id, complaint["status"], new_status, notes or None, actor_name(), updated))
        create_notification(conn, target_department_id, complaint_row_id,
                            f"Complaint {complaint['complaint_id']} status updated to {new_status}.")
        conn.commit()
    flash("Complaint updated successfully.", "success")
    return redirect(url_for("main.admin_dashboard"))


@main_bp.route("/admin/departments", methods=["POST"])
@main_admin_required
def update_department():
    require_csrf()
    action = request.form.get("action", "")
    name = request.form.get("name", "").strip()[:100]
    with connect_db() as conn:
        if action == "toggle":
            dept_id = request.form.get("department_id", "")
            dept = conn.execute("SELECT name,active FROM departments WHERE id=?", (dept_id,)).fetchone()
            if not dept:
                abort(404)
            if dept["name"] == "Other Department" and dept["active"]:
                abort(400, description="Other Department fallback cannot be deactivated.")
            conn.execute("UPDATE departments SET active=? WHERE id=?", (0 if dept["active"] else 1, dept_id))
        elif action == "mapping":
            category = request.form.get("category", "").strip()[:100]
            dept_id = request.form.get("department_id", "")
            dept = conn.execute("SELECT id,active FROM departments WHERE id=?", (dept_id,)).fetchone()
            if not category or not dept or not dept["active"]:
                abort(400, description="Choose a category and active department.")
            conn.execute("""
                INSERT INTO category_mappings(category,department_id,updated_at) VALUES (?,?,?)
                ON CONFLICT(category) DO UPDATE SET department_id=excluded.department_id,updated_at=excluded.updated_at
            """, (category, dept_id, now_iso()))
        else:
            abort(400, description="Unknown department action.")
        conn.commit()
    flash("Department settings saved.", "success")
    return redirect(url_for("main.admin_dashboard"))


@main_bp.route("/admin/department-admins", methods=["POST"])
@main_admin_required
def manage_department_admin():
    require_csrf()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    dept_id = request.form.get("department_id", "").strip()
    action = request.form.get("action", "create")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,50}", username):
        abort(400, description="Username must be 3-50 letters, digits, dots, underscores, or hyphens.")
    with connect_db() as conn:
        if action == "toggle":
            user_id = request.form.get("user_id", "")
            user = conn.execute("SELECT active FROM users WHERE id=?", (user_id,)).fetchone()
            if not user: abort(404)
            conn.execute("UPDATE users SET active=? WHERE id=?", (0 if user["active"] else 1, user_id))
        elif action == "create":
            if len(password) < 12:
                abort(400, description="Use a department admin password of at least 12 characters.")
            dept = conn.execute("SELECT id,active FROM departments WHERE id=?", (dept_id,)).fetchone()
            if not dept or not dept["active"]:
                abort(400, description="Choose an active department.")
            try:
                conn.execute("""
                    INSERT INTO users(username,password_hash,role,department_id,active,created_at)
                    VALUES (?,?, 'department_admin', ?,1,?)
                """, (username, generate_password_hash(password, method="pbkdf2:sha256"), dept_id, now_iso()))
            except sqlite3.IntegrityError:
                abort(409, description="That username already exists.")
        elif action == "reset":
            if len(password) < 12:
                abort(400, description="Use a password of at least 12 characters.")
            user_id = request.form.get("user_id", "")
            conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                         (generate_password_hash(password, method="pbkdf2:sha256"), user_id))
        else:
            abort(400)
        conn.commit()
    flash("Department admin account updated.", "success")
    return redirect(url_for("main.admin_dashboard"))


@main_bp.route("/admin/workers", methods=["POST"])
@admin_required
def manage_worker():
    require_csrf()
    role = current_role()
    name = request.form.get("name", "").strip()[:100]
    username = request.form.get("username", "").strip()[:50]
    phone = request.form.get("phone", "").strip()[:30]
    dept_id = request.form.get("department_id", "").strip()
    action = request.form.get("action", "create")
    with connect_db() as conn:
        if action == "toggle":
            worker_id = request.form.get("worker_id", "")
            worker = conn.execute("SELECT department_id,active FROM field_workers WHERE id=?", (worker_id,)).fetchone()
            if not worker: abort(404)
            if role == "department_admin" and worker["department_id"] != session.get("department_id"):
                abort(403)
            conn.execute("UPDATE field_workers SET active=? WHERE id=?", (0 if worker["active"] else 1, worker_id))
        else:
            if not name or not re.fullmatch(r"[A-Za-z0-9_.-]{3,50}", username):
                abort(400, description="Worker name and valid username are required.")
            if role == "department_admin":
                dept_id = str(session.get("department_id"))
            dept = conn.execute("SELECT id,active FROM departments WHERE id=?", (dept_id,)).fetchone()
            if not dept or not dept["active"]:
                abort(400, description="Choose an active department.")
            try:
                conn.execute("""
                    INSERT INTO field_workers(name,username,phone,department_id,active,created_at)
                    VALUES (?,?,?,?,1,?)
                """, (name, username, phone or None, dept_id, now_iso()))
            except sqlite3.IntegrityError:
                abort(409, description="Worker username already exists.")
        conn.commit()
    flash("Field worker saved.", "success")
    return redirect(url_for("main.admin_dashboard"))


@main_bp.route("/admin/private-upload/<filename>")
@admin_required
def private_upload(filename):
    # Only serve known files, and restrict department admins to files attached to their department's complaints.
    safe = secure_filename(filename)
    if safe != filename:
        abort(404)
    with connect_db() as conn:
        row = conn.execute("""
            SELECT department_id FROM complaints
            WHERE image_filename=? OR resolution_proof_filename=?
        """, (filename, filename)).fetchone()
    if not row:
        abort(404)
    if current_role() == "department_admin" and row["department_id"] != session.get("department_id"):
        abort(403)
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)


@main_bp.route("/admin/history/<int:complaint_row_id>")
@admin_required
def complaint_history(complaint_row_id):
    with connect_db() as conn:
        complaint = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_row_id,)).fetchone()
        if not complaint: abort(404)
        if current_role() == "department_admin" and complaint["department_id"] != session.get("department_id"):
            abort(403)
        history = conn.execute("""
            SELECT * FROM complaint_history WHERE complaint_id=? ORDER BY id
        """, (complaint_row_id,)).fetchall()
    return jsonify({"complaint_id": complaint["complaint_id"], "history": [dict(h) for h in history]})


@main_bp.route("/admin/analytics")
@main_admin_required
def analytics():
    with connect_db() as conn:
        rows = conn.execute("""
            SELECT d.name, COUNT(c.id) AS total,
            SUM(CASE WHEN c.status='Resolved' THEN 1 ELSE 0 END) AS resolved,
            SUM(CASE WHEN c.status IN ('Submitted','Pending','Under Review') THEN 1 ELSE 0 END) AS pending
            FROM departments d LEFT JOIN complaints c ON c.department_id=d.id
            GROUP BY d.id ORDER BY d.name
        """).fetchall()
    return jsonify({"departments": [dict(r) for r in rows]})
