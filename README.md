# CivicSense AI — Department-Wise Complaint Management Upgrade

This upgrade builds on the existing Flask/SQLite project. It does not include your private `.env`, live SQLite database, uploaded complaint photos, or `.git` history.

## Features included
- Citizen complaint registration and tracking with existing `CIV-XXXXXXXX` ID format.
- Six seeded departments, including a protected `Other Department` fallback.
- Configurable category-to-department mappings.
- Department admin accounts with hashed passwords and department-scoped complaint access.
- Main Admin login through environment credentials.
- Field-worker records and department-restricted assignments.
- Complaint status history, handling/resolution notes, resolution proof images.
- Department notifications and department-wise analytics.
- CSRF protection for citizen/admin forms, session protections, prepared SQL statements, image-signature validation, and authenticated complaint-image access.
- Additive SQLite migration and backup utility.

No trained NLP classifier is included. Routing uses editable category mappings and falls back to Other Department for unknown categories or inactive mappings.

## Windows setup
1. Make a separate backup copy of your existing project and `database/civicsense.db`.
2. Copy the files from this upgrade into the matching folders of your existing project. Do not delete your existing database.
3. Create a local `.env` file from `.env.example` and replace the placeholders.
4. Create a main-admin password hash in PowerShell:
   `python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash(input('Password: '), method='pbkdf2:sha256'))"`
   Put the printed hash in `ADMIN_PASSWORD_HASH`. Use a strong password and don't share it.
5. In VS Code terminal, activate your existing virtual environment, then run:
   `python -m pip install -r requirements.txt`
6. Back up and migrate the existing database:
   `python migrate_db.py`
   The script creates a timestamped backup before applying the additive migration.
7. Run:
   `python app.py`
8. Open `http://127.0.0.1:5000/` for citizens and `http://127.0.0.1:5000/admin/login` for admin login.

## Create department admins
After migration, run:
`python create_department_admin.py`
Choose a unique username, a password of at least 12 characters, and a department ID shown by the script. Passwords are stored as Werkzeug hashes. You can also create, activate/deactivate, or reset department admin accounts from the Main Admin dashboard.

## Test
Install requirements and run:
`python -m pytest -q`
Tests use a temporary SQLite database, not your live database.

## Important notes
- Root `app.py` is the supported entry point. `backend/app.py` is only a compatibility shim.
- Keep `.env`, `database/*.db`, and uploaded complaint images out of GitHub.
- The current schema migration preserves existing complaint rows and adds new tables/columns. For a legacy SQLite table, department references are validated with triggers because SQLite cannot add a foreign-key constraint to an existing column with `ALTER TABLE`.
- Public deployment on ephemeral storage is only suitable for demos. Real civic use needs durable database and uploaded-file storage, HTTPS, backups, access policies, and operational monitoring.
- Automated tests must be run in your own environment after installing dependencies. Code compilation was checked during preparation, but full Flask tests could not run in this environment because Flask was not installed and package downloads were unavailable.
