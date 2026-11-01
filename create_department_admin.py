"""Create a department admin securely without storing plaintext passwords."""
import getpass
from backend.routes import connect_db, init_db, now_iso
from werkzeug.security import generate_password_hash


def main():
    init_db()
    username = input("New department admin username: ").strip()
    password = getpass.getpass("Password (minimum 12 characters): ")
    confirm = getpass.getpass("Confirm password: ")
    if len(password) < 12:
        raise SystemExit("Password must be at least 12 characters.")
    if password != confirm:
        raise SystemExit("Passwords do not match.")
    with connect_db() as conn:
        departments = conn.execute(
            "SELECT id, name FROM departments WHERE active=1 ORDER BY name"
        ).fetchall()
        for d in departments:
            print(f"{d['id']}: {d['name']}")
        raw_id = input("Department ID: ").strip()
        if not raw_id.isdigit() or int(raw_id) not in {d['id'] for d in departments}:
            raise SystemExit("Choose a valid active department ID.")
        conn.execute("""
            INSERT INTO users(username,password_hash,role,department_id,active,created_at)
            VALUES (?,?,'department_admin',?,1,?)
        """, (username, generate_password_hash(password, method="pbkdf2:sha256"), int(raw_id), now_iso()))
        conn.commit()
    print("Department admin created. No plaintext password was saved.")


if __name__ == "__main__":
    main()
