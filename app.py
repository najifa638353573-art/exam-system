import os
import re
import sqlite3
import csv
from datetime import datetime, timezone
from functools import wraps
from io import StringIO

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
    abort,
    Response,
)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "exam.db")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

SITE_NAME = "SH TECH ZONE"
ADMIN_USERNAME = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASS", "admin123")
FACEBOOK_URL = "https://www.facebook.com/SH.TECH.ZONE/"
WHATSAPP_NUMBER = "+8801609450034"
TELEGRAM_URL = "https://t.me/MR_Expart_SH"
DEFAULT_EXAM_LABEL = "SSC"

BD_PHONE_RE = re.compile(r"^01\d{9}$")              # 01XXXXXXXXX
BD_WHATSAPP_RE = re.compile(r"^\+8801\d{9}$")       # +8801XXXXXXXXX


# ---------------- DB helpers ----------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        pass
    return conn

def utcnow():
    return datetime.now(timezone.utc)

def col_exists(conn, table, col):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == col for r in rows)

def ensure_column(conn, table, col, coltype_sql):
    if not col_exists(conn, table, col):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype_sql}")

def table_sql(conn, name):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return (row["sql"] or "") if row else ""

def migrate_users_drop_status_check(conn):
    """
    Older DBs may have: status TEXT ... CHECK(status IN (...))
    We need dynamic statuses. We'll rebuild table without CHECK.
    """
    sql = table_sql(conn, "users")
    if "CHECK (status IN" not in sql:
        return

    # create new table without CHECK constraint
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        student_code TEXT UNIQUE,
        phone TEXT UNIQUE NOT NULL,
        whatsapp TEXT NOT NULL,
        subject_id INTEGER,
        batch_id INTEGER,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        is_blocked INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'waiting',
        location TEXT NOT NULL,
        current_status TEXT NOT NULL,
        education_level TEXT NOT NULL,
        FOREIGN KEY (subject_id) REFERENCES subjects(id),
        FOREIGN KEY (batch_id) REFERENCES batches(id)
    )
    """)

    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    # copy common cols
    common = [c for c in [
        "id","name","phone","whatsapp","subject_id","batch_id","password_hash","created_at",
        "is_blocked","status","location","current_status","education_level"
    ] if c in cols]
    col_list = ", ".join(common)
    conn.execute(f"INSERT INTO users_new ({col_list}) SELECT {col_list} FROM users")

    conn.execute("DROP TABLE users")
    conn.execute("ALTER TABLE users_new RENAME TO users")

def init_db():
    """Create tables and run lightweight migrations safely."""
    conn = get_db()
    cur = conn.cursor()

    # --- core tables ---
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        name TEXT,
        profile_image TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS subjects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        image_path TEXT,
        description TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id INTEGER,
        name TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        UNIQUE(subject_id, name),
        FOREIGN KEY (subject_id) REFERENCES subjects(id)
    )
    """)

    # Student status options (admin can add)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS status_options (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """)

    # Application status options (admin can add)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS application_status_options (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        student_code TEXT UNIQUE,
        phone TEXT UNIQUE NOT NULL,
        whatsapp TEXT NOT NULL,
        subject_id INTEGER,
        batch_id INTEGER,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        is_blocked INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'registered',
        location TEXT NOT NULL DEFAULT '-',
        current_status TEXT NOT NULL DEFAULT '-',
        education_level TEXT NOT NULL DEFAULT '-',
        profile_image TEXT,
        FOREIGN KEY (subject_id) REFERENCES subjects(id),
        FOREIGN KEY (batch_id) REFERENCES batches(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        note TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS admin_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        note TEXT NOT NULL,
        created_at TEXT NOT NULL,
        created_by TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE,
        phone TEXT UNIQUE,
        role TEXT,
        password_hash TEXT,
        created_at TEXT NOT NULL,
        profile_image TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS exams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id INTEGER,
        batch_id INTEGER,
        title TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT 'SSC',
        duration_minutes INTEGER NOT NULL DEFAULT 20,
        is_active INTEGER NOT NULL DEFAULT 1,
        visibility TEXT NOT NULL DEFAULT 'approved', -- 'registered' or 'approved'
        created_at TEXT NOT NULL,
        FOREIGN KEY (subject_id) REFERENCES subjects(id),
        FOREIGN KEY (batch_id) REFERENCES batches(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        exam_id INTEGER NOT NULL,
        q_no INTEGER,
        q_type TEXT NOT NULL DEFAULT 'mcq' CHECK (q_type IN ('mcq','text')),
        question TEXT NOT NULL,
        opt_a TEXT,
        opt_b TEXT,
        opt_c TEXT,
        opt_d TEXT,
        correct CHAR(1) CHECK (correct IN ('A','B','C','D')),
        correct_text TEXT,
        FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE CASCADE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        exam_id INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        submitted_at TEXT,
        score INTEGER,
        total INTEGER,
        pending_written INTEGER NOT NULL DEFAULT 0,
        allowed_extra_attempts INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE CASCADE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS attempt_answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id INTEGER NOT NULL,
        question_id INTEGER NOT NULL,
        chosen_option CHAR(1),
        text_answer TEXT,
        is_correct INTEGER, -- NULL = pending review (written)
        FOREIGN KEY (attempt_id) REFERENCES attempts(id) ON DELETE CASCADE,
        FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS written_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_answer_id INTEGER NOT NULL,
        reviewer_employee_id INTEGER,
        decided_correct INTEGER NOT NULL,
        note TEXT,
        reviewed_at TEXT NOT NULL,
        FOREIGN KEY (attempt_answer_id) REFERENCES attempt_answers(id) ON DELETE CASCADE,
        FOREIGN KEY (reviewer_employee_id) REFERENCES employees(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        whatsapp TEXT,
        desired_subject_id INTEGER,
        location TEXT,
        current_status TEXT,
        education_level TEXT,
        password_hash TEXT,
        note TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        FOREIGN KEY (desired_subject_id) REFERENCES subjects(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS courses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id INTEGER,
        title TEXT NOT NULL,
        description TEXT,
        details TEXT,
        image_filename TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(subject_id) REFERENCES subjects(id)
    )
    """)

    conn.commit()

    # ---- migrations / ensure columns on older DBs ----
    # Some old DBs had CHECK(status IN ...) in users; rebuild to remove CHECK
    try:
        migrate_users_drop_status_check(conn)
    except Exception:
        pass

    # subjects
    ensure_column(conn, "subjects", "image_path", "TEXT")
    ensure_column(conn, "subjects", "description", "TEXT")

    # users
    ensure_column(conn, "users", "student_code", "TEXT")
    ensure_column(conn, "users", "location", "TEXT")
    ensure_column(conn, "users", "current_status", "TEXT")
    ensure_column(conn, "users", "education_level", "TEXT")
    ensure_column(conn, "users", "is_blocked", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "users", "profile_image", "TEXT")

    # admins / employees
    ensure_column(conn, "admins", "name", "TEXT")
    ensure_column(conn, "admins", "profile_image", "TEXT")
    ensure_column(conn, "admins", "created_at", "TEXT")
    ensure_column(conn, "employees", "email", "TEXT")
    ensure_column(conn, "employees", "password_hash", "TEXT")
    ensure_column(conn, "employees", "phone", "TEXT")
    ensure_column(conn, "employees", "profile_image", "TEXT")

    # exams visibility
    ensure_column(conn, "exams", "visibility", "TEXT NOT NULL DEFAULT 'approved'")

    # courses
    ensure_column(conn, "courses", "subject_id", "INTEGER")
    ensure_column(conn, "courses", "description", "TEXT")
    ensure_column(conn, "courses", "details", "TEXT")
    ensure_column(conn, "courses", "image_filename", "TEXT")

    # applications
    ensure_column(conn, "applications", "desired_subject_id", "INTEGER")
    ensure_column(conn, "applications", "password_hash", "TEXT")
    ensure_column(conn, "applications", "status", "TEXT NOT NULL DEFAULT 'pending'")

    conn.commit()

    # ---- seed defaults (idempotent) ----
    # default admin (table might not have created_at in older DB)
    cur.execute("SELECT 1 FROM admins WHERE username=?", (ADMIN_USERNAME,))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO admins (username, password_hash, name, created_at) VALUES (?,?,?,?)",
            (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD), "Admin", utcnow().isoformat()),
        )

    # seed subjects
    default_subjects = ["Basic Computer", "Digital Marketing", "Web Development", "Graphic Design", "Canva Design"]
    for s in default_subjects:
        cur.execute("SELECT 1 FROM subjects WHERE name=?", (s,))
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO subjects (name, is_active, created_at) VALUES (?,?,?)",
                (s, 1, utcnow().isoformat()),
            )

    # seed student statuses
    for st in ["registered", "approved", "rejected", "blocked"]:
        cur.execute("SELECT 1 FROM status_options WHERE name=?", (st,))
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO status_options (name, is_active, created_at) VALUES (?,?,?)",
                (st, 1, utcnow().isoformat()),
            )

    # seed application statuses
    for st in ["pending", "approved", "rejected", "in_review"]:
        cur.execute("SELECT 1 FROM application_status_options WHERE name=?", (st,))
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO application_status_options (name, is_active, created_at) VALUES (?,?,?)",
                (st, 1, utcnow().isoformat()),
            )

    conn.commit()

    # seed sample exam + questions if none
    cur.execute("SELECT 1 FROM exams LIMIT 1")
    if cur.fetchone() is None:
        dm = conn.execute("SELECT id FROM subjects WHERE name=?", ("Digital Marketing",)).fetchone()
        dm_id = dm["id"] if dm else None
        cur.execute(
            "INSERT INTO exams (subject_id, batch_id, title, label, duration_minutes, is_active, visibility, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (dm_id, None, "Digital Marketing Basics - Set 1", DEFAULT_EXAM_LABEL, 15, 1, "registered", utcnow().isoformat()),
        )
        exam_id = cur.lastrowid

        seed = [
            (30, "mcq", "Removing an ad account from Business Manager—",
             "Deletes the account", "Transfers ownership", "Removes access only", "Pauses ads permanently", "C", None),
            (31, "mcq", "Google Tag Manager is mainly used to—",
             "Track ads", "Manage scripts centrally", "Design websites", "Improve SEO", "B", None),
            (32, "mcq", "GTM container is installed on—",
             "Facebook Page", "Ads Manager", "Website code", "Business Manager", "C", None),
            (33, "text", "Written: Explain what is UTM parameter (short).",
             None, None, None, None, None, "Any short explanation is acceptable"),
        ]
        for q_no, q_type, q, a, b, c, d, ans, ctext in seed:
            cur.execute(
                """INSERT INTO questions (exam_id, q_no, q_type, question, opt_a, opt_b, opt_c, opt_d, correct, correct_text)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (exam_id, q_no, q_type, q, a, b, c, d, ans, ctext),
            )

    # backfill student codes for existing users
    try:
        rows = cur.execute("SELECT id FROM users WHERE student_code IS NULL OR student_code='' ").fetchall()
        for r in rows:
            cur.execute("UPDATE users SET student_code=? WHERE id=?", (f"STU{int(r['id']):05d}", int(r["id"])))
    except Exception:
        pass

    conn.commit()
    conn.close()

# ---------------- auth decorators ----------------
def student_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper
def employee_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("employee_id"):
            return redirect(url_for("employee_login"))
        return fn(*args, **kwargs)
    return wrapper






def staff_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("admin") or session.get("employee_id"):
            return fn(*args, **kwargs)
        return redirect(url_for("admin_login"))
    return wrapper

# ---------------- shared helpers ----------------
def require_student_ok():
    uid = session.get("user_id")
    if not uid:
        return False
    conn = get_db()
    row = conn.execute("SELECT is_blocked FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    if not row:
        session.clear()
        return False
    if row["is_blocked"]:
        session.clear()
        flash("আপনার অ্যাকাউন্ট ব্লক করা। Admin এর সাথে যোগাযোগ করুন।", "danger")
        return False
    return True

def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def get_active_statuses(conn):
    return conn.execute(
        "SELECT name FROM status_options WHERE is_active=1 ORDER BY id ASC"
    ).fetchall()

def get_active_application_statuses(conn):
    return conn.execute(
        "SELECT name FROM application_status_options WHERE is_active=1 ORDER BY id ASC"
    ).fetchall()

def user_can_view_exam(user_status: str, exam_visibility: str) -> bool:
    """Exam visibility:
    - 'registered': any logged-in student except rejected/blocked
    - 'approved'   : only approved students
    """
    vis = (exam_visibility or "approved").lower()
    st = (user_status or "registered").lower()
    if vis == "registered":
        return st not in ("rejected", "blocked")
    return st == "approved"

def user_can_list_any_exam(user_status: str) -> bool:
    """Whether student can see the exam list page."""
    st = (user_status or "registered").lower()
    return st not in ("rejected", "blocked")


# ---------------- student pages ----------------
@app.route("/")
def home():
    return render_template("student/home.html", site_name=SITE_NAME)


@app.route("/subjects")
def subjects_page():
    conn = get_db()
    subjects = conn.execute("SELECT * FROM subjects WHERE is_active=1 ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("student/subjects.html", site_name=SITE_NAME, subjects=subjects, facebook_url=FACEBOOK_URL, telegram_url=TELEGRAM_URL)

@app.route("/apply/<int:subject_id>", methods=["GET", "POST"])
@student_required
def apply_subject(subject_id):
    if not require_student_ok():
        return redirect(url_for("login"))
    conn = get_db()
    subject = conn.execute("SELECT * FROM subjects WHERE id=? AND is_active=1", (subject_id,)).fetchone()
    if not subject:
        conn.close()
        abort(404)

    if request.method == "POST":
        # if logged in, use profile fields; else ask minimal
        uid = session.get("user_id")
        if uid:
            u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not u:
                session.pop("user_id", None)
                conn.close()
                return redirect(url_for("login"))
            name = u["name"]
            phone = u["phone"]
            whatsapp = u["whatsapp"]
            location = u["location"]
            current_status = u["current_status"]
            education_level = u["education_level"]
        else:
            name = request.form.get("name", "").strip()
            phone = request.form.get("phone", "").strip()
            whatsapp = request.form.get("whatsapp", "").strip()
            location = request.form.get("location", "").strip()
            current_status = request.form.get("current_status", "").strip()
            education_level = request.form.get("education_level", "").strip()

        note = (request.form.get("note") or "").strip() or None

        conn.execute(
            """INSERT INTO applications (name, phone, whatsapp, location, current_status, education_level, desired_subject_id, note, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (name, phone, whatsapp, location, current_status, education_level, subject_id, note, "pending", utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
        flash(f"Application submitted ✅ WhatsApp এ নক দাও: {WHATSAPP_NUMBER}", "success")
        return redirect(url_for("subjects_page"))

    conn.close()
    return render_template("student/apply_subject.html", subject=subject)

@app.route("/apply", methods=["GET", "POST"])
def apply():
    # Deprecated: keep endpoint but force registration/login first
    if not session.get("user_id"):
        flash("Apply করতে আগে Register/Login করতে হবে।", "warning")
        return redirect(url_for("register"))

    conn = get_db()
    subjects = conn.execute("SELECT * FROM subjects WHERE is_active=1 ORDER BY id DESC").fetchall()
    conn.close()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        whatsapp = request.form.get("whatsapp", "").strip() or None
        desired_subject_id = request.form.get("desired_subject_id", "").strip() or None
        location = request.form.get("location", "").strip() or None
        current_status = request.form.get("current_status", "").strip() or None
        education_level = request.form.get("education_level", "").strip() or None
        note = request.form.get("note", "").strip() or None

        if not name or not phone:
            flash("Name + Phone লাগবে।", "warning")
            return redirect(url_for("apply"))

        conn = get_db()
        conn.execute(
            """INSERT INTO applications
               (name, phone, whatsapp, desired_subject_id, location, current_status, education_level, note, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (name, phone, whatsapp, int(desired_subject_id) if desired_subject_id else None,
             location, current_status, education_level, note, "pending", utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
        flash("Application submitted ✅ (Admin review করবে)", "success")
        return redirect(url_for("home"))

    return render_template("student/apply.html", site_name=SITE_NAME, subjects=subjects)

@app.route("/register", methods=["GET", "POST"])
def register():
    conn = get_db()
    subjects = conn.execute("SELECT * FROM subjects WHERE is_active=1 ORDER BY id DESC").fetchall()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        whatsapp = request.form.get("whatsapp", "").strip()
        subject_id = request.form.get("subject_id", "").strip()
        current_status = request.form.get("current_status", "").strip()
        education_level = request.form.get("education_level", "").strip()
        location = request.form.get("location", "").strip()
        password = request.form.get("password", "")

        if not all([name, phone, whatsapp, subject_id, current_status, education_level, location, password]):
            flash("All fields are required", "danger")
            return render_template("student/register.html", subjects=subjects)

        if not whatsapp.startswith("+880"):
            flash("WhatsApp must start with +880", "danger")
            return render_template("student/register.html", subjects=subjects)

        if not str(subject_id).isdigit():
            flash("Invalid subject", "danger")
            return render_template("student/register.html", subjects=subjects)

        sid = int(subject_id)

        # Create or update user with PENDING status (can login, but exams locked until approved)
        u = conn.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
        if u:
            flash("Phone already registered. Please login.", "warning")
            conn.close()
            return redirect(url_for("login"))

        ph = generate_password_hash(password)
        conn.execute(
            """INSERT INTO users (name, phone, whatsapp, subject_id, batch_id, current_status, education_level, location,
                                 password_hash, status, is_blocked, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, phone, whatsapp, sid, None, current_status, education_level, location, ph, "pending", 0, utcnow().isoformat()),
        )
        user_id = conn.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()["id"]

        # generate student ID code
        conn.execute("UPDATE users SET student_code=? WHERE id=?", (f"STU{int(user_id):05d}", user_id))


        # Create application record (pending)
        conn.execute(
            """INSERT INTO applications (name, phone, whatsapp, location, current_status, education_level, desired_subject_id, note, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (name, phone, whatsapp, location, current_status, education_level, sid, "Auto from registration", "pending", utcnow().isoformat()),
        )

        conn.commit()
        conn.close()

        # Auto-login
        session["user_id"] = user_id
        flash(f"Registration submitted ✅ এখন WhatsApp এ নক দাও: {WHATSAPP_NUMBER}", "success")
        return redirect(url_for("dashboard"))

    conn.close()
    return render_template("student/register.html", subjects=subjects)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        # Admin credentials
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session.clear()
            session["admin"] = True
            flash("Admin login successful ✅", "success")
            return redirect(url_for("admin_subjects"))

        conn = get_db()

        # Employee login by email
        if "@" in username:
            e = conn.execute("SELECT * FROM employees WHERE email=?", (username.lower(),)).fetchone()
            if e and e["password_hash"] and check_password_hash(e["password_hash"], password):
                session.clear()
                session["employee_id"] = e["id"]
                session["employee_name"] = e["name"]
                conn.close()
                flash("Employee login successful ✅", "success")
                return redirect(url_for("employee_written_queue"))
        else:
            # Student login by phone
            u = conn.execute("SELECT * FROM users WHERE phone=?", (username,)).fetchone()
            if u and check_password_hash(u["password_hash"], password):
                if u["is_blocked"]:
                    conn.close()
                    flash("Account is blocked", "danger")
                    return render_template("student/login.html")
                session.clear()
                session["user_id"] = u["id"]
                conn.close()
                flash("Login successful ✅", "success")
                return redirect(url_for("dashboard"))

        conn.close()
        flash("Invalid login", "danger")
        return render_template("student/login.html")

    return render_template("student/login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logout ✅", "success")
    return redirect(url_for("home"))

@app.route("/profile", methods=["GET", "POST"])
@student_required
def profile():
    if not require_student_ok():
        return redirect(url_for("login"))

    uid = session["user_id"]
    conn = get_db()
    user = conn.execute(
        """SELECT u.*, s.name AS subject_name
           FROM users u LEFT JOIN subjects s ON s.id=u.subject_id
           WHERE u.id=?""",
        (uid,),
    ).fetchone()

    if not user:
        conn.close()
        session.clear()
        return redirect(url_for("login"))

    if request.method == "POST":
        # allow update: name, whatsapp, location, current_status, education_level
        name = request.form.get("name", "").strip()
        whatsapp = request.form.get("whatsapp", "").strip()
        location = request.form.get("location", "").strip()
        current_status = request.form.get("current_status", "").strip()
        education_level = request.form.get("education_level", "").strip()
        # profile image (optional)
        img_file = request.files.get("profile_image")
        img_filename = None
        if img_file and img_file.filename:
            ext = os.path.splitext(img_file.filename.lower())[1]
            if ext not in (".png", ".jpg", ".jpeg"):
                conn.close()
                flash("Profile image must be PNG/JPG", "danger")
                return redirect(url_for("profile"))
            up_dir = os.path.join(BASE_DIR, "static", "uploads")
            os.makedirs(up_dir, exist_ok=True)
            img_filename = f"user_{uid}{ext}"
            img_path = os.path.join(up_dir, img_filename)
            img_file.save(img_path)



        if not all([name, whatsapp, location, current_status, education_level]):
            conn.close()
            flash("সব ফিল্ড পূরণ করো।", "danger")
            return redirect(url_for("profile"))

        if not BD_WHATSAPP_RE.match(whatsapp):
            conn.close()
            flash("WhatsApp format must be: +8801XXXXXXXXX", "danger")
            return redirect(url_for("profile"))
        if img_filename:
            conn.execute(
                """UPDATE users
                   SET name=?, whatsapp=?, location=?, current_status=?, education_level=?, profile_image=?
                   WHERE id=?""",
                (name, whatsapp, location, current_status, education_level, img_filename, uid),
            )
        else:
            conn.execute(
                """UPDATE users
                   SET name=?, whatsapp=?, location=?, current_status=?, education_level=?
                   WHERE id=?""",
                (name, whatsapp, location, current_status, education_level, uid),
            )
        conn.commit()
        # reload
        user = conn.execute(
            """SELECT u.*, s.name AS subject_name
               FROM users u LEFT JOIN subjects s ON s.id=u.subject_id
               WHERE u.id=?""",
            (uid,),
        ).fetchone()
        conn.close()
        session["user_name"] = user["name"]
        flash("Profile updated ✅", "success")
        return redirect(url_for("profile"))

    conn.close()
    return render_template("student/profile.html", site_name=SITE_NAME, user=user)

@app.route("/dashboard")
@student_required
def dashboard():
    if not require_student_ok():
        return redirect(url_for("login"))

    uid = session["user_id"]
    conn = get_db()

    user = conn.execute(
        """SELECT u.*, s.name AS subject_name
           FROM users u
           LEFT JOIN subjects s ON s.id=u.subject_id
           WHERE u.id=?""",
        (uid,),
    ).fetchone()

    if not user:
        conn.close()
        session.clear()
        return redirect(url_for("login"))

    st = user["status"]
    can_view_exams = user_can_list_any_exam(st)

    exams = []
    if can_view_exams:
        exams = conn.execute(
            """SELECT e.*, s.name AS subject_name
               FROM exams e
               LEFT JOIN subjects s ON s.id=e.subject_id
               WHERE e.is_active=1
                 AND e.subject_id=?
                 AND (e.batch_id IS NULL OR e.batch_id = ?)
                 AND (
                        e.visibility='registered'
                        OR (e.visibility='approved' AND ?='approved')
                     )
               ORDER BY e.id DESC""",
            (user["subject_id"], user["batch_id"], st),
        ).fetchall()

    attempts = conn.execute(
        """SELECT a.*, e.title, e.label, e.duration_minutes
           FROM attempts a JOIN exams e ON e.id=a.exam_id
           WHERE a.user_id=? AND a.submitted_at IS NOT NULL
           ORDER BY a.submitted_at DESC""",
        (uid,),
    ).fetchall()

    conn.close()
    return render_template(
        "student/dashboard.html",
        site_name=SITE_NAME,
        user=user,
        can_view_exams=can_view_exams,
        exams=exams,
        attempts=attempts,
    )


def get_last_attempt(conn, uid, exam_id):
    return conn.execute(
        "SELECT * FROM attempts WHERE user_id=? AND exam_id=? ORDER BY id DESC LIMIT 1",
        (uid, exam_id),
    ).fetchone()

def can_start_exam(conn, uid, exam_id):
    last = get_last_attempt(conn, uid, exam_id)
    if last is None:
        return True, None
    if last["submitted_at"] is None:
        return True, last
    if last["allowed_extra_attempts"] > 0:
        return True, last
    return False, last

@app.route("/exam/<int:exam_id>/start", methods=["POST"])
@student_required
def start_exam(exam_id):
    if not require_student_ok():
        return redirect(url_for("login"))

    uid = session["user_id"]
    conn = get_db()

    user = conn.execute("SELECT subject_id, batch_id, status FROM users WHERE id=?", (uid,)).fetchone()
    if not user or not user_can_list_any_exam(user["status"]):
        conn.close()
        flash("আপনার অ্যাকাউন্ট স্ট্যাটাসের কারণে exam দেখা যাবে না।", "warning")
        return redirect(url_for("dashboard"))

    exam = conn.execute(
        "SELECT * FROM exams WHERE id=? AND is_active=1",
        (exam_id,),
    ).fetchone()

    if not exam or exam["subject_id"] != user["subject_id"]:
        conn.close()
        abort(404)

    # visibility gate
    if not user_can_view_exam(user["status"], exam["visibility"]):
        conn.close()
        flash("এই exam দেখতে/দিতে আপনার অনুমতি নেই।", "warning")
        return redirect(url_for("dashboard"))

    # batch gate

        conn.close()
        abort(404)

    # batch gate
    if exam["batch_id"] is not None and user["batch_id"] != exam["batch_id"]:
        conn.close()
        abort(404)

    ok, last = can_start_exam(conn, uid, exam_id)
    if not ok:
        conn.close()
        flash("এই এক্সাম একবারই দেওয়া যাবে। Retake লাগলে Admin permission দরকার।", "warning")
        return redirect(url_for("dashboard"))

    if last and last["submitted_at"] is None:
        conn.close()
        return redirect(url_for("take_exam", exam_id=exam_id, attempt_id=last["id"]))

    conn.execute(
        "INSERT INTO attempts (user_id, exam_id, started_at, allowed_extra_attempts) VALUES (?,?,?,?)",
        (uid, exam_id, utcnow().isoformat(), (last["allowed_extra_attempts"] if last else 0)),
    )
    conn.commit()
    attempt_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.close()
    return redirect(url_for("take_exam", exam_id=exam_id, attempt_id=attempt_id))

@app.route("/exam/<int:exam_id>/<int:attempt_id>")
@student_required
def take_exam(exam_id, attempt_id):
    if not require_student_ok():
        return redirect(url_for("login"))

    uid = session["user_id"]
    conn = get_db()

    user = conn.execute("SELECT subject_id, batch_id, status FROM users WHERE id=?", (uid,)).fetchone()
    if not user or not user_can_list_any_exam(user["status"]):
        conn.close()
        flash("আপনার অ্যাকাউন্ট স্ট্যাটাসের কারণে exam দেখা যাবে না।", "warning")
        return redirect(url_for("dashboard"))

    exam = conn.execute(
        """SELECT e.*, s.name AS subject_name
           FROM exams e LEFT JOIN subjects s ON s.id=e.subject_id
           WHERE e.id=? AND e.is_active=1""",
        (exam_id,),
    ).fetchone()

    # visibility gate
    if not user_can_view_exam(user["status"], exam["visibility"]):
        conn.close()
        flash("এই exam দেখতে/দিতে আপনার অনুমতি নেই।", "warning")
        return redirect(url_for("dashboard"))

    attempt = conn.execute(
        "SELECT * FROM attempts WHERE id=? AND user_id=? AND exam_id=?",
        (attempt_id, uid, exam_id),
    ).fetchone()

    if not exam or not attempt:
        conn.close()
        abort(404)

    if exam["subject_id"] != user["subject_id"]:
        conn.close()
        abort(404)

    if exam["batch_id"] is not None and user["batch_id"] != exam["batch_id"]:
        conn.close()
        abort(404)

    if attempt["submitted_at"] is not None:
        conn.close()
        return redirect(url_for("result", attempt_id=attempt_id))

    questions = conn.execute(
        "SELECT * FROM questions WHERE exam_id=? ORDER BY COALESCE(q_no, id)",
        (exam_id,),
    ).fetchall()

    conn.close()
    if not questions:
        flash("এই এক্সামে কোনো প্রশ্ন নেই।", "warning")
        return redirect(url_for("dashboard"))

    started_at = datetime.fromisoformat(attempt["started_at"])
    duration = int(exam["duration_minutes"])
    elapsed = (utcnow() - started_at).total_seconds()
    remaining = max(0, int(duration * 60 - elapsed))

    return render_template(
        "student/exam.html",
        site_name=SITE_NAME,
        exam=exam,
        attempt_id=attempt_id,
        questions=questions,
        remaining_seconds=remaining,
    )

@app.post("/exam/<int:attempt_id>/submit")
@student_required
def submit_exam(attempt_id):
    if not require_student_ok():
        return redirect(url_for("login"))

    uid = session["user_id"]
    conn = get_db()

    user = conn.execute("SELECT status FROM users WHERE id=?", (uid,)).fetchone()
    if not user or not user_can_list_any_exam(user["status"]):
        conn.close()
        flash("আপনার অ্যাকাউন্ট স্ট্যাটাসের কারণে exam দেওয়া যাবে না।", "warning")
        return redirect(url_for("dashboard"))

    attempt = conn.execute(
        "SELECT * FROM attempts WHERE id=? AND user_id=?",
        (attempt_id, uid),
    ).fetchone()
    if not attempt:
        conn.close()
        abort(404)

    if attempt["submitted_at"] is not None:
        conn.close()
        return redirect(url_for("result", attempt_id=attempt_id))

    exam = conn.execute("SELECT * FROM exams WHERE id=?", (attempt["exam_id"],)).fetchone()

    if not user_can_view_exam(user["status"], exam["visibility"]):
        conn.close()
        flash("এই exam দিতে আপনার অনুমতি নেই।", "warning")
        return redirect(url_for("dashboard"))
    questions = conn.execute(
        "SELECT * FROM questions WHERE exam_id=? ORDER BY COALESCE(q_no, id)",
        (attempt["exam_id"],),
    ).fetchall()

    started_at = datetime.fromisoformat(attempt["started_at"])
    duration = int(exam["duration_minutes"])
    elapsed = (utcnow() - started_at).total_seconds()

    conn.execute("DELETE FROM attempt_answers WHERE attempt_id=?", (attempt_id,))

    score = 0
    total = len(questions)
    pending_written = 0

    for q in questions:
        chosen = None
        text_ans = None
        is_correct = 0

        if q["q_type"] == "mcq":
            chosen = request.form.get(f"q_{q['id']}")
            is_correct = 1 if (chosen and q["correct"] and chosen == q["correct"]) else 0
            if is_correct:
                score += 1
            conn.execute(
                """INSERT INTO attempt_answers (attempt_id, question_id, chosen_option, text_answer, is_correct)
                   VALUES (?,?,?,?,?)""",
                (attempt_id, q["id"], chosen, None, is_correct),
            )
        else:
            # written -> pending review by employee/admin
            text_ans = request.form.get(f"q_{q['id']}_text")
            pending_written += 1
            conn.execute(
                """INSERT INTO attempt_answers (attempt_id, question_id, chosen_option, text_answer, is_correct)
                   VALUES (?,?,?,?,NULL)""",
                (attempt_id, q["id"], None, text_ans),
            )

    conn.execute(
        "UPDATE attempts SET submitted_at=?, score=?, total=?, pending_written=? WHERE id=?",
        (utcnow().isoformat(), score, total, pending_written, attempt_id),
    )

    if attempt["allowed_extra_attempts"] > 0:
        conn.execute(
            "UPDATE attempts SET allowed_extra_attempts = allowed_extra_attempts - 1 WHERE id=?",
            (attempt_id,),
        )

    conn.commit()
    conn.close()

    if elapsed > duration * 60:
        flash("সময় শেষ হয়ে গেছিল—তবুও সাবমিট নেওয়া হয়েছে।", "warning")
    else:
        flash("Submitted ✅", "success")

    return redirect(url_for("result", attempt_id=attempt_id))

@app.route("/result/<int:attempt_id>")
@student_required
def result(attempt_id):
    if not require_student_ok():
        return redirect(url_for("login"))

    uid = session["user_id"]
    conn = get_db()

    attempt = conn.execute(
        """SELECT a.*, e.title, e.label, e.duration_minutes, s.name AS subject_name
           FROM attempts a
           JOIN exams e ON e.id=a.exam_id
           LEFT JOIN subjects s ON s.id=e.subject_id
           WHERE a.id=? AND a.user_id=?""",
        (attempt_id, uid),
    ).fetchone()

    if not attempt or attempt["submitted_at"] is None:
        conn.close()
        abort(404)

    rows = conn.execute(
        """SELECT q.*, ans.id AS ans_id, ans.chosen_option, ans.text_answer, ans.is_correct
           FROM attempt_answers ans
           JOIN questions q ON q.id=ans.question_id
           WHERE ans.attempt_id=?
           ORDER BY COALESCE(q.q_no, q.id)""",
        (attempt_id,),
    ).fetchall()

    conn.close()
    return render_template(
        "student/result.html",
        site_name=SITE_NAME,
        attempt=attempt,
        rows=rows,
    )


# ---------------- admin auth ----------------

@app.route("/admin")
@admin_required
def admin_dashboard():
    # Admin landing page (kept for backward compatibility)
    return redirect(url_for("admin_subjects"))

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    # Backward-compatible URL: redirect to unified login
    return redirect(url_for("login"))

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    flash("Admin logout ✅", "success")
    return redirect(url_for("admin_login"))


@app.route("/admin/profile", methods=["GET", "POST"])
@admin_required
def admin_profile():
    conn = get_db()
    admin = conn.execute("SELECT * FROM admins WHERE username=?", (ADMIN_USERNAME,)).fetchone()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip() or (admin["name"] if admin else "Admin")
        img = request.files.get("profile_image")
        img_filename = admin["profile_image"] if admin else None
        if img and img.filename:
            ext = os.path.splitext(img.filename.lower())[1]
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                conn.close()
                flash("Profile image must be PNG/JPG/WEBP", "danger")
                return redirect(url_for("admin_profile"))
            up_dir = os.path.join(BASE_DIR, "static", "uploads")
            os.makedirs(up_dir, exist_ok=True)
            img_filename = f"admin_{int(utcnow().timestamp())}{ext}"
            img.save(os.path.join(up_dir, img_filename))

        conn.execute(
            "UPDATE admins SET name=?, profile_image=? WHERE username=?",
            (name, img_filename, ADMIN_USERNAME),
        )
        conn.commit()
        admin = conn.execute("SELECT * FROM admins WHERE username=?", (ADMIN_USERNAME,)).fetchone()
        conn.close()
        flash("Admin profile updated ✅", "success")
        return redirect(url_for("admin_profile"))

    conn.close()
    return render_template("admin/admin_profile.html", site_name=SITE_NAME, admin=admin)

@app.route("/employee/profile", methods=["GET", "POST"])
@employee_required
def employee_profile():
    emp_id = session.get("employee_id")
    conn = get_db()
    emp = conn.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip() or (emp["name"] if emp else "")
        phone = (request.form.get("phone") or "").strip() or None
        role = (request.form.get("role") or "").strip() or None
        img = request.files.get("profile_image")
        img_filename = emp["profile_image"] if emp else None
        if img and img.filename:
            ext = os.path.splitext(img.filename.lower())[1]
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                conn.close()
                flash("Profile image must be PNG/JPG/WEBP", "danger")
                return redirect(url_for("employee_profile"))
            up_dir = os.path.join(BASE_DIR, "static", "uploads")
            os.makedirs(up_dir, exist_ok=True)
            img_filename = f"emp_{emp_id}_{int(utcnow().timestamp())}{ext}"
            img.save(os.path.join(up_dir, img_filename))

        conn.execute(
            "UPDATE employees SET name=?, phone=?, role=?, profile_image=? WHERE id=?",
            (name, phone, role, img_filename, emp_id),
        )
        conn.commit()
        conn.close()
        flash("Employee profile updated ✅", "success")
        return redirect(url_for("employee_profile"))

    conn.close()
    return render_template("employee/profile.html", site_name=SITE_NAME, emp=emp)

# ---------------- employee auth (for written review) ----------------
@app.route("/employee/login", methods=["GET", "POST"])
def employee_login():
    # Backward-compatible URL: redirect to unified login
    return redirect(url_for("login"))

@app.route("/employee/logout")
def employee_logout():
    session.pop("employee_id", None)
    session.pop("employee_name", None)
    flash("Employee logout ✅", "success")
    return redirect(url_for("employee_login"))

@app.route("/employee/written")
@employee_required
def employee_written_queue():
    conn = get_db()
    pending = conn.execute(
        """SELECT ans.id AS ans_id, a.id AS attempt_id, u.name AS student_name, u.phone,
                  e.title AS exam_title, q.q_no, q.question, ans.text_answer, q.correct_text
           FROM attempt_answers ans
           JOIN attempts a ON a.id=ans.attempt_id
           JOIN users u ON u.id=a.user_id
           JOIN questions q ON q.id=ans.question_id
           JOIN exams e ON e.id=a.exam_id
           WHERE q.q_type='text' AND ans.is_correct IS NULL
           ORDER BY a.submitted_at DESC"""
    ).fetchall()
    conn.close()
    return render_template("employee/written_queue.html", site_name=SITE_NAME, pending=pending)

@app.post("/employee/written/<int:ans_id>/decide")
@employee_required
def employee_written_decide(ans_id):
    decided = request.form.get("decided")  # '1' correct, '0' wrong
    note = (request.form.get("note") or "").strip() or None
    if decided not in ("0","1"):
        abort(400)
    decided_i = int(decided)

    conn = get_db()
    row = conn.execute(
        """SELECT ans.attempt_id
           FROM attempt_answers ans
           JOIN questions q ON q.id=ans.question_id
           WHERE ans.id=? AND q.q_type='text'""",
        (ans_id,),
    ).fetchone()
    if not row:
        conn.close()
        abort(404)

    conn.execute("UPDATE attempt_answers SET is_correct=? WHERE id=?", (decided_i, ans_id))
    conn.execute(
        """INSERT INTO written_reviews (attempt_answer_id, reviewer_employee_id, decided_correct, note, reviewed_at)
           VALUES (?,?,?,?,?)""",
        (ans_id, session.get("employee_id"), decided_i, note, utcnow().isoformat()),
    )

    att = conn.execute("SELECT score, pending_written FROM attempts WHERE id=?", (row["attempt_id"],)).fetchone()
    new_pending = max(0, int(att["pending_written"]) - 1) if att else 0
    new_score = int(att["score"] or 0) + (1 if decided_i == 1 else 0) if att else (1 if decided_i == 1 else 0)
    conn.execute(
        "UPDATE attempts SET pending_written=?, score=? WHERE id=?",
        (new_pending, new_score, row["attempt_id"]),
    )

    conn.commit()
    conn.close()
    flash("Reviewed ✅", "success")
    return redirect(url_for("employee_written_queue"))




# ---------------- admin pages (left menu order: Subjects -> Batches -> Exams -> ...) ----------------
@app.route("/admin/subjects")
@staff_required
def admin_subjects():
    conn = get_db()
    subjects = conn.execute("SELECT * FROM subjects ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin/subjects.html", site_name=SITE_NAME, subjects=subjects)

@app.route("/admin/batches")
@staff_required
def admin_batches():
    conn = get_db()
    batches = conn.execute(
        """SELECT b.*, s.name AS subject_name
           FROM batches b LEFT JOIN subjects s ON s.id=b.subject_id
           ORDER BY b.id DESC"""
    ).fetchall()
    subjects = conn.execute("SELECT * FROM subjects ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin/batches.html", site_name=SITE_NAME, batches=batches, subjects=subjects)

@app.route("/admin/exams")
@staff_required
def admin_exams():
    conn = get_db()
    subjects = conn.execute("SELECT * FROM subjects ORDER BY id DESC").fetchall()
    batches = conn.execute(
        """SELECT b.*, s.name AS subject_name
           FROM batches b LEFT JOIN subjects s ON s.id=b.subject_id
           ORDER BY b.id DESC"""
    ).fetchall()
    exams = conn.execute(
        """SELECT e.*, s.name AS subject_name, b.name AS batch_name
           FROM exams e
           LEFT JOIN subjects s ON s.id=e.subject_id
           LEFT JOIN batches b ON b.id=e.batch_id
           ORDER BY e.id DESC"""
    ).fetchall()
    conn.close()
    return render_template("admin/exams.html", site_name=SITE_NAME, subjects=subjects, batches=batches, exams=exams)

# ---- subjects CRUD
@app.route("/admin/subjects/add", methods=["GET", "POST"])
@admin_required
def admin_subject_add():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        description = (request.form.get("description") or "").strip() or None
        is_active = 1 if request.form.get("is_active") == "on" else 0

        img = request.files.get("image")
        image_path = None
        if img and img.filename:
            ext = os.path.splitext(img.filename.lower())[1]
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                flash("Image must be PNG/JPG/WEBP", "danger")
                return redirect(url_for("admin_subject_add"))
            up_dir = os.path.join(BASE_DIR, "static", "uploads")
            os.makedirs(up_dir, exist_ok=True)
            image_path = f"subject_{int(utcnow().timestamp())}{ext}"
            img.save(os.path.join(up_dir, image_path))

        if not name:
            flash("Subject name required", "danger")
            return redirect(url_for("admin_subject_add"))

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO subjects (name, description, image_path, is_active, created_at) VALUES (?,?,?,?,?)",
                (name, description, image_path, is_active, utcnow().isoformat()),
            )
            conn.commit()
            flash("Subject added ✅", "success")
        except sqlite3.IntegrityError:
            flash("Subject already exists", "danger")
        conn.close()
        return redirect(url_for("admin_subjects"))

    return render_template("admin/subject_form.html", site_name=SITE_NAME, mode="add", s=None)

@app.route("/admin/subjects/<int:subject_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_subject_edit(subject_id):
    conn = get_db()
    s = conn.execute("SELECT * FROM subjects WHERE id=?", (subject_id,)).fetchone()
    if not s:
        conn.close()
        abort(404)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        description = (request.form.get("description") or "").strip() or None
        is_active = 1 if request.form.get("is_active") == "on" else 0

        img = request.files.get("image")
        image_path = s["image_path"]
        if img and img.filename:
            ext = os.path.splitext(img.filename.lower())[1]
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                conn.close()
                flash("Image must be PNG/JPG/WEBP", "danger")
                return redirect(url_for("admin_subject_edit", subject_id=subject_id))
            up_dir = os.path.join(BASE_DIR, "static", "uploads")
            os.makedirs(up_dir, exist_ok=True)
            image_path = f"subject_{int(utcnow().timestamp())}{ext}"
            img.save(os.path.join(up_dir, image_path))

        conn.execute(
            "UPDATE subjects SET name=?, description=?, image_path=?, is_active=? WHERE id=?",
            (name, description, image_path, is_active, subject_id),
        )
        conn.commit()
        conn.close()
        flash("Subject updated ✅", "success")
        return redirect(url_for("admin_subjects"))

    conn.close()
    return render_template("admin/subject_form.html", site_name=SITE_NAME, mode="edit", s=s)

@app.post("/admin/subjects/<int:subject_id>/delete")
@staff_required
def admin_subject_delete(subject_id):
    conn = get_db()
    conn.execute("DELETE FROM subjects WHERE id=?", (subject_id,))
    conn.commit()
    conn.close()
    flash("Subject deleted ✅", "success")
    return redirect(url_for("admin_subjects"))

# ---- batches CRUD
@app.route("/admin/batches/add", methods=["GET", "POST"])
@staff_required
def admin_batch_add():
    conn = get_db()
    subjects = conn.execute("SELECT * FROM subjects ORDER BY id DESC").fetchall()

    if request.method == "POST":
        subject_id = int(request.form.get("subject_id") or 0)
        name = request.form.get("name", "").strip()
        is_active = 1 if request.form.get("is_active") == "on" else 0
        img = request.files.get("image")
        img_filename = None
        if img and img.filename:
            ext = os.path.splitext(img.filename.lower())[1]
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                flash("Image must be PNG/JPG/WEBP", "danger")
                return redirect(url_for("admin_subject_edit", subject_id=subject_id))
            up_dir = os.path.join(BASE_DIR, "static", "uploads")
            os.makedirs(up_dir, exist_ok=True)
            img_filename = f"subject_{int(utcnow().timestamp())}{ext}"
            img.save(os.path.join(up_dir, img_filename))
        if not subject_id or not name:
            conn.close()
            flash("Subject + Batch name required", "danger")
            return redirect(url_for("admin_batch_add"))
        try:
            conn.execute(
                "INSERT INTO batches (subject_id, name, is_active, created_at) VALUES (?,?,?,?)",
                (subject_id, name, is_active, utcnow().isoformat()),
            )
            conn.commit()
            flash("Batch added ✅", "success")
        except sqlite3.IntegrityError:
            flash("Batch already exists for this subject", "danger")
        conn.close()
        return redirect(url_for("admin_batches"))

    conn.close()
    return render_template("admin/batch_form.html", site_name=SITE_NAME, mode="add", b=None, subjects=subjects)

@app.route("/admin/batches/<int:batch_id>/edit", methods=["GET", "POST"])
@staff_required
def admin_batch_edit(batch_id):
    conn = get_db()
    b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    subjects = conn.execute("SELECT * FROM subjects ORDER BY id DESC").fetchall()
    if not b:
        conn.close()
        abort(404)

    if request.method == "POST":
        subject_id = int(request.form.get("subject_id") or 0)
        name = request.form.get("name", "").strip()
        is_active = 1 if request.form.get("is_active") == "on" else 0
        img = request.files.get("image")
        img_filename = None
        if img and img.filename:
            ext = os.path.splitext(img.filename.lower())[1]
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                flash("Image must be PNG/JPG/WEBP", "danger")
                return redirect(url_for("admin_subject_edit", subject_id=subject_id))
            up_dir = os.path.join(BASE_DIR, "static", "uploads")
            os.makedirs(up_dir, exist_ok=True)
            img_filename = f"subject_{int(utcnow().timestamp())}{ext}"
            img.save(os.path.join(up_dir, img_filename))
        conn.execute(
            "UPDATE batches SET subject_id=?, name=?, is_active=? WHERE id=?",
            (subject_id, name, is_active, batch_id),
        )
        conn.commit()
        conn.close()
        flash("Batch updated ✅", "success")
        return redirect(url_for("admin_batches"))

    conn.close()
    return render_template("admin/batch_form.html", site_name=SITE_NAME, mode="edit", b=b, subjects=subjects)

@app.post("/admin/batches/<int:batch_id>/delete")
@staff_required
def admin_batch_delete(batch_id):
    conn = get_db()
    conn.execute("DELETE FROM batches WHERE id=?", (batch_id,))
    conn.commit()
    conn.close()
    flash("Batch deleted ✅", "success")
    return redirect(url_for("admin_batches"))

# ---- exams CRUD
@app.route("/admin/exams/add", methods=["GET", "POST"])
@admin_required
def admin_exam_add():
    conn = get_db()
    subjects = conn.execute("SELECT * FROM subjects ORDER BY id DESC").fetchall()
    batches = conn.execute(
        """SELECT b.*, s.name AS subject_name
           FROM batches b LEFT JOIN subjects s ON s.id=b.subject_id
           ORDER BY b.id DESC"""
    ).fetchall()

    if request.method == "POST":
        subject_id = int(request.form.get("subject_id") or 0)
        batch_id_raw = request.form.get("batch_id")
        batch_id = int(batch_id_raw) if (batch_id_raw and batch_id_raw != "ALL") else None

        title = (request.form.get("title") or "").strip()
        label = (request.form.get("label") or "").strip() or DEFAULT_EXAM_LABEL
        duration = int(request.form.get("duration_minutes") or 20)
        is_active = 1 if request.form.get("is_active") == "on" else 0

        visibility = (request.form.get("visibility") or "approved").strip().lower()
        if visibility not in ("registered", "approved"):
            visibility = "approved"

        if not title or not subject_id:
            conn.close()
            flash("Subject + Title required", "danger")
            return redirect(url_for("admin_exam_add"))

        # if a batch selected, it must belong to subject
        if batch_id is not None:
            b = conn.execute("SELECT subject_id FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not b or b["subject_id"] != subject_id:
                conn.close()
                flash("Batch must belong to selected Subject", "danger")
                return redirect(url_for("admin_exam_add"))

        conn.execute(
            """INSERT INTO exams (subject_id, batch_id, title, label, duration_minutes, visibility, is_active, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (subject_id, batch_id, title, label, duration, visibility, is_active, utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
        flash("Exam added ✅", "success")
        return redirect(url_for("admin_exams"))

    conn.close()
    return render_template(
        "admin/exam_form.html",
        site_name=SITE_NAME,
        mode="add",
        exam=None,
        subjects=subjects,
        batches=batches,
    )

@app.route("/admin/exams/<int:exam_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_exam_edit(exam_id):
    conn = get_db()
    exam = conn.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
    subjects = conn.execute("SELECT * FROM subjects ORDER BY id DESC").fetchall()
    batches = conn.execute(
        """SELECT b.*, s.name AS subject_name
           FROM batches b LEFT JOIN subjects s ON s.id=b.subject_id
           ORDER BY b.id DESC"""
    ).fetchall()
    if not exam:
        conn.close()
        abort(404)

    if request.method == "POST":
        subject_id = int(request.form.get("subject_id") or 0)
        batch_id_raw = request.form.get("batch_id")
        batch_id = int(batch_id_raw) if (batch_id_raw and batch_id_raw != "ALL") else None

        title = (request.form.get("title") or "").strip()
        label = (request.form.get("label") or "").strip() or DEFAULT_EXAM_LABEL
        duration = int(request.form.get("duration_minutes") or 20)
        is_active = 1 if request.form.get("is_active") == "on" else 0

        visibility = (request.form.get("visibility") or (exam["visibility"] if "visibility" in exam.keys() else "approved")).strip().lower()
        if visibility not in ("registered", "approved"):
            visibility = "approved"

        if batch_id is not None:
            b = conn.execute("SELECT subject_id FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not b or b["subject_id"] != subject_id:
                conn.close()
                flash("Batch must belong to selected Subject", "danger")
                return redirect(url_for("admin_exam_edit", exam_id=exam_id))

        conn.execute(
            """UPDATE exams
               SET subject_id=?, batch_id=?, title=?, label=?, duration_minutes=?, visibility=?, is_active=?
               WHERE id=?""",
            (subject_id, batch_id, title, label, duration, visibility, is_active, exam_id),
        )
        conn.commit()
        conn.close()
        flash("Exam updated ✅", "success")
        return redirect(url_for("admin_exams"))

    conn.close()
    return render_template(
        "admin/exam_form.html",
        site_name=SITE_NAME,
        mode="edit",
        exam=exam,
        subjects=subjects,
        batches=batches,
    )

@app.post("/admin/exams/<int:exam_id>/delete")
@staff_required
def admin_exam_delete(exam_id):
    conn = get_db()
    conn.execute("DELETE FROM exams WHERE id=?", (exam_id,))
    conn.commit()
    conn.close()
    flash("Exam deleted ✅", "success")
    return redirect(url_for("admin_exams"))

# ---- questions

@app.route("/admin/exams/<int:exam_id>/bulk", methods=["GET", "POST"])
@staff_required
def admin_bulk_questions(exam_id):
    conn = get_db()
    exam = conn.execute(
        """SELECT e.*, s.name AS subject_name
           FROM exams e LEFT JOIN subjects s ON s.id=e.subject_id
           WHERE e.id=?""",
        (exam_id,),
    ).fetchone()
    if not exam:
        conn.close()
        abort(404)

    if request.method == "POST":
        raw = request.form.get("bulk", "")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        added = 0

        i = 0
        while i < len(lines):
            # Expect: Qxx. question
            qline = lines[i]
            qno = None
            question = qline
            m = re.match(r"^Q\s*(\d+)\.?\s*(.*)$", qline, flags=re.I)
            if m:
                qno = int(m.group(1))
                question = m.group(2).strip() or qline
            i += 1

            # If next lines are options A-D -> MCQ
            opt_a = opt_b = opt_c = opt_d = None
            correct = None
            correct_text = None
            q_type = "mcq"

            # parse options
            def grab_opt(prefix):
                nonlocal i
                if i < len(lines) and re.match(rf"^{prefix}\)", lines[i], flags=re.I):
                    val = re.sub(rf"^{prefix}\)\s*", "", lines[i], flags=re.I).strip()
                    i += 1
                    return val
                return None

            opt_a = grab_opt("A")
            opt_b = grab_opt("B")
            opt_c = grab_opt("C")
            opt_d = grab_opt("D")

            # If no A option -> treat as written block: next line "Answer:" optional
            if opt_a is None:
                q_type = "text"
                # look for Answer:
                if i < len(lines) and lines[i].lower().startswith("answer"):
                    correct_text = lines[i].split(":", 1)[-1].strip()
                    i += 1
                # skip blank separators
            else:
                # find Correct Answer line
                while i < len(lines) and not lines[i].lower().startswith("q"):
                    if lines[i].lower().startswith("correct"):
                        # "Correct Answer: C"
                        val = lines[i].split(":", 1)[-1].strip().upper()
                        correct = val[:1] if val else None
                        i += 1
                        break
                    else:
                        i += 1

            if q_type == "mcq" and correct not in ("A","B","C","D"):
                correct = "A"

            conn.execute(
                """INSERT INTO questions (exam_id, q_no, q_type, question, opt_a, opt_b, opt_c, opt_d, correct, correct_text)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (exam_id, qno, q_type, question, opt_a, opt_b, opt_c, opt_d, correct, correct_text),
            )
            added += 1

            # move to next Q if current line already is Q; loop continues

        conn.commit()
        conn.close()
        flash(f"Bulk added: {added} questions ✅", "success")
        return redirect(url_for("admin_questions", exam_id=exam_id))

    conn.close()
    sample = """Q30. Removing an ad account from Business Manager—
A) Deletes the account
B) Transfers ownership
C) Removes access only
D) Pauses ads permanently
Correct Answer: C

Q31. Google Tag Manager is mainly used to—
A) Track ads
B) Manage scripts centrally
C) Design websites
D) Improve SEO
Correct Answer: B
"""
    return render_template("admin/bulk_questions.html", exam=exam, sample=sample)

@app.route("/admin/exams/<int:exam_id>/questions")
@staff_required
def admin_questions(exam_id):
    conn = get_db()
    exam = conn.execute(
        """SELECT e.*, s.name AS subject_name
           FROM exams e LEFT JOIN subjects s ON s.id=e.subject_id
           WHERE e.id=?""",
        (exam_id,),
    ).fetchone()
    questions = conn.execute(
        "SELECT * FROM questions WHERE exam_id=? ORDER BY COALESCE(q_no, id)",
        (exam_id,),
    ).fetchall()
    conn.close()
    return render_template("admin/questions.html", site_name=SITE_NAME, exam=exam, questions=questions)

@app.route("/admin/exams/<int:exam_id>/questions/add", methods=["GET", "POST"])
@staff_required
def admin_question_add(exam_id):
    conn = get_db()
    exam = conn.execute(
        """SELECT e.*, s.name AS subject_name
           FROM exams e LEFT JOIN subjects s ON s.id=e.subject_id
           WHERE e.id=?""",
        (exam_id,),
    ).fetchone()
    if not exam:
        conn.close()
        abort(404)

    if request.method == "POST":
        q_no = request.form.get("q_no") or None
        q_type_ui = request.form.get("q_type", "mcq")
        q_type = "mcq" if q_type_ui == "mcq" else "text"
        question = request.form.get("question", "").strip()

        if not question:
            conn.close()
            flash("Question required", "danger")
            return redirect(url_for("admin_question_add", exam_id=exam_id))

        if q_type == "mcq":
            a = request.form.get("opt_a", "").strip()
            b = request.form.get("opt_b", "").strip()
            c = request.form.get("opt_c", "").strip()
            d = request.form.get("opt_d", "").strip()
            correct = request.form.get("correct", "").strip().upper()
            correct_text = None
            if correct not in ("A","B","C","D") or not all([a,b,c,d]):
                conn.close()
                flash("MCQ: Options A-D + Correct (A/B/C/D) required", "danger")
                return redirect(url_for("admin_question_add", exam_id=exam_id))
        else:
            a=b=c=d=None
            correct=None
            correct_text = request.form.get("correct_text", "").strip() or None
            # for written we don't force correct_text (employee will judge)
            # but admin can write reference answer if wanted.

        conn.execute(
            """INSERT INTO questions (exam_id, q_no, q_type, question, opt_a, opt_b, opt_c, opt_d, correct, correct_text)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (exam_id, int(q_no) if q_no else None, q_type, question, a, b, c, d, correct, correct_text),
        )
        conn.commit()
        conn.close()
        flash("Question added ✅", "success")
        return redirect(url_for("admin_questions", exam_id=exam_id))

    conn.close()
    return render_template("admin/question_form.html", site_name=SITE_NAME, mode="add", exam=exam, q=None)

@app.route("/admin/questions/<int:q_id>/edit", methods=["GET", "POST"])
@staff_required
def admin_question_edit(q_id):
    conn = get_db()
    q = conn.execute("SELECT * FROM questions WHERE id=?", (q_id,)).fetchone()
    if not q:
        conn.close()
        abort(404)
    exam = conn.execute(
        """SELECT e.*, s.name AS subject_name
           FROM exams e LEFT JOIN subjects s ON s.id=e.subject_id
           WHERE e.id=?""",
        (q["exam_id"],),
    ).fetchone()

    if request.method == "POST":
        q_no = request.form.get("q_no") or None
        q_type_ui = request.form.get("q_type", "mcq")
        q_type = "mcq" if q_type_ui == "mcq" else "text"
        question = request.form.get("question", "").strip()

        if q_type == "mcq":
            a = request.form.get("opt_a", "").strip()
            b = request.form.get("opt_b", "").strip()
            c = request.form.get("opt_c", "").strip()
            d = request.form.get("opt_d", "").strip()
            correct = request.form.get("correct", "").strip().upper()
            correct_text = None
        else:
            a=b=c=d=None
            correct=None
            correct_text = request.form.get("correct_text", "").strip() or None

        conn.execute(
            """UPDATE questions
               SET q_no=?, q_type=?, question=?, opt_a=?, opt_b=?, opt_c=?, opt_d=?, correct=?, correct_text=?
               WHERE id=?""",
            (int(q_no) if q_no else None, q_type, question, a, b, c, d, correct, correct_text, q_id),
        )
        conn.commit()
        conn.close()
        flash("Question updated ✅", "success")
        return redirect(url_for("admin_questions", exam_id=exam["id"]))

    conn.close()
    return render_template("admin/question_form.html", site_name=SITE_NAME, mode="edit", exam=exam, q=q)

@app.post("/admin/questions/<int:q_id>/delete")
@staff_required
def admin_question_delete(q_id):
    conn = get_db()
    q = conn.execute("SELECT exam_id FROM questions WHERE id=?", (q_id,)).fetchone()
    if not q:
        conn.close()
        abort(404)
    conn.execute("DELETE FROM questions WHERE id=?", (q_id,))
    conn.commit()
    conn.close()
    flash("Question deleted ✅", "success")
    return redirect(url_for("admin_questions", exam_id=q["exam_id"]))

# ---- applications (registration pending)
@app.route("/admin/applications")
@admin_required
def admin_applications():
    conn = get_db()
    apps = conn.execute(
        """SELECT a.*, s.name AS subject_name
           FROM applications a
           LEFT JOIN subjects s ON s.id=a.desired_subject_id
           ORDER BY a.id DESC"""
    ).fetchall()
    conn.close()
    return render_template("admin/applications.html", site_name=SITE_NAME, apps=apps)

@app.post("/admin/applications/<int:app_id>/set_status")
@admin_required
def admin_application_status(app_id):
    status = (request.form.get("status") or "pending").strip()
    conn = get_db()
    ok = conn.execute("SELECT 1 FROM application_status_options WHERE name=? AND is_active=1", (status,)).fetchone()
    if not ok:
        conn.close()
        flash("Invalid application status option", "danger")
        return redirect(url_for("admin_applications"))
    if status == "approved":
        # convert application -> user
        a = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if not a:
            conn.close()
            abort(404)

        # prevent duplicate
        ex = conn.execute("SELECT 1 FROM users WHERE phone=?", (a["phone"],)).fetchone()
        if ex:
            conn.execute("UPDATE applications SET status=? WHERE id=?", ("approved", app_id))
            conn.commit()
            conn.close()
            flash("Already existed user; application marked approved.", "warning")
            return redirect(url_for("admin_applications"))

        # create user with approved status
        conn.execute(
            """INSERT INTO users
               (name, phone, whatsapp, subject_id, batch_id, password_hash, created_at,
                is_blocked, status, location, current_status, education_level)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                a["name"], a["phone"], a["whatsapp"] or "",
                a["desired_subject_id"], None, a["password_hash"] or generate_password_hash("123456"),
                utcnow().isoformat(),
                0, "approved",
                a["location"] or "-", a["current_status"] or "-", a["education_level"] or "-"
            ),
        )
        conn.execute("UPDATE applications SET status=? WHERE id=?", ("approved", app_id))
        conn.commit()
        conn.close()
        flash("Approved ✅ Student account created", "success")
        return redirect(url_for("admin_students"))

    # normal update
    conn.execute("UPDATE applications SET status=? WHERE id=?", (status, app_id))
    conn.commit()
    conn.close()
    flash("Application updated ✅", "success")
    return redirect(url_for("admin_applications"))

# ---- students
@app.route("/admin/students")
@admin_required
def admin_students():
    conn = get_db()
    students = conn.execute(
        """SELECT u.*, s.name AS subject_name, b.name AS batch_name
           FROM users u
           LEFT JOIN subjects s ON s.id=u.subject_id
           LEFT JOIN batches b ON b.id=u.batch_id
           ORDER BY u.id DESC"""
    ).fetchall()
    statuses = get_active_statuses(conn)

    subjects = conn.execute("SELECT * FROM subjects ORDER BY id DESC").fetchall()
    batches = conn.execute(
        """SELECT b.*, s.name AS subject_name
           FROM batches b LEFT JOIN subjects s ON s.id=b.subject_id
           ORDER BY b.id DESC"""
    ).fetchall()

    conn.close()
    return render_template(
        "admin/students.html",
        site_name=SITE_NAME,
        students=students,
        statuses=statuses,
        subjects=subjects,
        batches=batches,
    )


@app.route("/admin/students/<int:user_id>")
@admin_required
def admin_student_profile(user_id):
    conn = get_db()
    u = conn.execute(
        """SELECT u.*, s.name AS subject_name, b.name AS batch_name
           FROM users u
           LEFT JOIN subjects s ON s.id=u.subject_id
           LEFT JOIN batches b ON b.id=u.batch_id
           WHERE u.id=?""",
        (user_id,),
    ).fetchone()
    if not u:
        conn.close()
        abort(404)

    try:
        notes = conn.execute(
            "SELECT id, note, created_at, COALESCE(created_by, '') AS created_by FROM admin_notes WHERE user_id=? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    except Exception:
        notes = []

    attempts = conn.execute(
        """SELECT a.*, e.title AS exam_title, e.label, s.name AS subject_name
           FROM attempts a
           JOIN exams e ON e.id=a.exam_id
           LEFT JOIN subjects s ON s.id=e.subject_id
           WHERE a.user_id=? AND a.submitted_at IS NOT NULL
           ORDER BY a.submitted_at DESC""",
        (user_id,),
    ).fetchall()

    statuses = get_active_statuses(conn)
    subjects = conn.execute("SELECT * FROM subjects ORDER BY id DESC").fetchall()
    batches = conn.execute(
        """SELECT b.*, s.name AS subject_name
           FROM batches b LEFT JOIN subjects s ON s.id=b.subject_id
           ORDER BY b.id DESC"""
    ).fetchall()

    conn.close()
    return render_template(
        "admin/student_profile.html",
        site_name=SITE_NAME,
        u=u,
        notes=notes,
        attempts=attempts,
        statuses=statuses,
        subjects=subjects,
        batches=batches,
    )


@app.post("/admin/students/<int:user_id>/toggle_block")
@admin_required
def admin_toggle_block(user_id):
    conn = get_db()
    row = conn.execute("SELECT is_blocked FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    new_val = 0 if row["is_blocked"] else 1
    conn.execute("UPDATE users SET is_blocked=? WHERE id=?", (new_val, user_id))
    conn.commit()
    conn.close()
    flash("Updated ✅", "success")
    return redirect(url_for("admin_students"))

@app.post("/admin/students/<int:user_id>/set_status")
@admin_required
def admin_set_student_status(user_id):
    status = request.form.get("status", "").strip()
    if not status:
        abort(400)
    conn = get_db()
    # validate against active list
    ok = conn.execute("SELECT 1 FROM status_options WHERE name=? AND is_active=1", (status,)).fetchone()
    if not ok:
        conn.close()
        flash("Invalid status option", "danger")
        return redirect(url_for("admin_student_profile", user_id=user_id))
    conn.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
    conn.commit()
    conn.close()
    flash("Student status updated ✅", "success")
    return redirect(url_for("admin_student_profile", user_id=user_id))

@app.post("/admin/students/<int:user_id>/add_note")
@admin_required
def admin_add_student_note(user_id):
    note = (request.form.get("note") or "").strip()
    if not note:
        flash("Note empty", "danger")
        return redirect(url_for("admin_student_profile", user_id=user_id))
    conn = get_db()
    conn.execute(
        "INSERT INTO admin_notes (user_id, note, created_at, created_by) VALUES (?,?,?,?)",
        (user_id, note, utcnow().isoformat(), "admin"),
    )
    conn.commit()
    conn.close()
    flash("Note added ✅", "success")
    return redirect(url_for("admin_student_profile", user_id=user_id))

# ---- status options
@app.post("/admin/students/<int:user_id>/set_subject_batch")
@admin_required
def admin_set_subject_batch(user_id):
    subject_id_raw = request.form.get("subject_id")
    batch_id_raw = request.form.get("batch_id")
    subject_id = int(subject_id_raw) if (subject_id_raw and subject_id_raw.isdigit()) else None
    batch_id = int(batch_id_raw) if (batch_id_raw and batch_id_raw.isdigit()) else None

    conn = get_db()
    u = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
    if not u:
        conn.close()
        abort(404)

    # validate subject
    if subject_id is not None:
        s = conn.execute("SELECT 1 FROM subjects WHERE id=?", (subject_id,)).fetchone()
        if not s:
            conn.close()
            flash("Invalid subject", "danger")
            return redirect(url_for("admin_student_profile", user_id=user_id))

    # validate batch belongs to subject (if both set)
    if batch_id is not None:
        b = conn.execute("SELECT subject_id FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            conn.close()
            flash("Invalid batch", "danger")
            return redirect(url_for("admin_student_profile", user_id=user_id))
        if subject_id is not None and b["subject_id"] != subject_id:
            conn.close()
            flash("Batch must belong to selected subject", "danger")
            return redirect(url_for("admin_student_profile", user_id=user_id))

    conn.execute("UPDATE users SET subject_id=?, batch_id=? WHERE id=?", (subject_id, batch_id, user_id))
    conn.commit()
    conn.close()
    flash("Subject/Batch updated ✅", "success")
    return redirect(url_for("admin_student_profile", user_id=user_id))



@app.post("/admin/api/users/<int:user_id>/status")
@admin_required
def admin_api_set_status(user_id):
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip()
    if not status:
        return jsonify({"ok": False, "error": "status required"}), 400
    conn = get_db()
    u = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
    if not u:
        conn.close()
        return jsonify({"ok": False, "error": "not found"}), 404
    conn.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.post("/admin/api/users/<int:user_id>/subject_batch")
@admin_required
def admin_api_set_subject_batch(user_id):
    data = request.get_json(silent=True) or {}
    subject_id = data.get("subject_id")
    batch_id = data.get("batch_id")

    conn = get_db()
    u = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
    if not u:
        conn.close()
        return jsonify({"ok": False, "error": "not found"}), 404

    subject_id_i = int(subject_id) if str(subject_id).isdigit() else None
    batch_id_i = int(batch_id) if str(batch_id).isdigit() else None

    if subject_id_i is not None:
        s = conn.execute("SELECT 1 FROM subjects WHERE id=?", (subject_id_i,)).fetchone()
        if not s:
            conn.close()
            return jsonify({"ok": False, "error": "invalid subject"}), 400

    if batch_id_i is not None:
        b = conn.execute("SELECT subject_id FROM batches WHERE id=?", (batch_id_i,)).fetchone()
        if not b:
            conn.close()
            return jsonify({"ok": False, "error": "invalid batch"}), 400
        if subject_id_i is not None and b["subject_id"] != subject_id_i:
            conn.close()
            return jsonify({"ok": False, "error": "batch mismatch"}), 400

    conn.execute("UPDATE users SET subject_id=?, batch_id=? WHERE id=?", (subject_id_i, batch_id_i, user_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.post("/admin/attempts/<int:attempt_id>/delete")
@admin_required
def admin_delete_attempt(attempt_id):
    conn = get_db()
    row = conn.execute("SELECT id FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    conn.execute("DELETE FROM attempts WHERE id=?", (attempt_id,))
    conn.commit()
    conn.close()
    flash("Attempt deleted ✅ (Student can retake now)", "success")
    return redirect(url_for("admin_attempts"))


# ---- status options (admin can add)
@app.route("/admin/status", methods=["GET", "POST"])
@admin_required
def admin_status_options():
    conn = get_db()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            conn.close()
            flash("Status name required", "danger")
            return redirect(url_for("admin_status_options"))
        try:
            conn.execute(
                "INSERT INTO status_options (name, is_active, created_at) VALUES (?,?,?)",
                (name, 1, utcnow().isoformat()),
            )
            conn.commit()
            flash("Status added ✅", "success")
        except sqlite3.IntegrityError:
            flash("Status already exists", "danger")
        conn.close()
        return redirect(url_for("admin_status_options"))

    statuses = conn.execute("SELECT * FROM status_options ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin/status.html", site_name=SITE_NAME, statuses=statuses)

@app.post("/admin/status/<int:status_id>/toggle")
@admin_required
def admin_status_toggle(status_id):
    conn = get_db()
    row = conn.execute("SELECT is_active FROM status_options WHERE id=?", (status_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    new_val = 0 if row["is_active"] else 1
    conn.execute("UPDATE status_options SET is_active=? WHERE id=?", (new_val, status_id))
    conn.commit()
    conn.close()
    flash("Updated ✅", "success")
    return redirect(url_for("admin_status_options"))


# ---- application status options
@app.route("/admin/application-status", methods=["GET", "POST"])
@admin_required
def admin_application_status_options():
    conn = get_db()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            conn.close()
            flash("Status name required", "danger")
            return redirect(url_for("admin_application_status_options"))
        try:
            conn.execute(
                "INSERT INTO application_status_options (name, is_active, created_at) VALUES (?,?,?)",
                (name, 1, utcnow().isoformat()),
            )
            conn.commit()
            flash("Application status added ✅", "success")
        except sqlite3.IntegrityError:
            flash("Status already exists", "danger")
        conn.close()
        return redirect(url_for("admin_application_status_options"))

    statuses = conn.execute("SELECT * FROM application_status_options ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin/application_status.html", site_name=SITE_NAME, statuses=statuses)

@app.post("/admin/application-status/<int:status_id>/toggle")
@admin_required
def admin_application_status_toggle(status_id):
    conn = get_db()
    row = conn.execute("SELECT is_active FROM application_status_options WHERE id=?", (status_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    new_val = 0 if row["is_active"] else 1
    conn.execute("UPDATE application_status_options SET is_active=? WHERE id=?", (new_val, status_id))
    conn.commit()
    conn.close()
    flash("Updated ✅", "success")
    return redirect(url_for("admin_application_status_options"))

# ---- employees
@app.route("/admin/employees", methods=["GET", "POST"])
@admin_required
def admin_employees():
    conn = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = (request.form.get("email", "") or "").strip().lower() or None
        phone = (request.form.get("phone", "") or "").strip() or None
        role = (request.form.get("role", "") or "").strip() or None
        password = (request.form.get("password", "") or "")
        if not name:
            conn.close()
            flash("Name required", "danger")
            return redirect(url_for("admin_employees"))
        try:
            ph = generate_password_hash(password) if password else None
            conn.execute(
                "INSERT INTO employees (name, email, phone, role, password_hash, created_at) VALUES (?,?,?,?,?,?)",
                (name, email, phone, role, ph, utcnow().isoformat()),
            )
            conn.commit()
            flash("Employee added ✅", "success")
        except sqlite3.IntegrityError:
            flash("Employee phone already exists", "danger")
    employees = conn.execute("SELECT * FROM employees ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin/employees.html", site_name=SITE_NAME, employees=employees)

@app.post("/admin/employees/<int:emp_id>/delete")
@admin_required
def admin_employee_delete(emp_id):
    conn = get_db()
    conn.execute("DELETE FROM employees WHERE id=?", (emp_id,))
    conn.commit()
    conn.close()
    flash("Deleted ✅", "success")
    return redirect(url_for("admin_employees"))

# ---- attempts
@app.route("/admin/attempts")
@admin_required
def admin_attempts():
    conn = get_db()
    rows = conn.execute(
        """SELECT a.*, u.name AS student_name, u.phone, e.title AS exam_title, e.label, s.name AS subject_name
           FROM attempts a
           JOIN users u ON u.id=a.user_id
           JOIN exams e ON e.id=a.exam_id
           LEFT JOIN subjects s ON s.id=e.subject_id
           ORDER BY a.started_at DESC"""
    ).fetchall()
    conn.close()
    return render_template("admin/attempts.html", site_name=SITE_NAME, rows=rows)

@app.route("/admin/attempts/<int:attempt_id>")
@admin_required
def admin_attempt_detail(attempt_id):
    conn = get_db()
    attempt = conn.execute(
        """SELECT a.*, u.name AS student_name, u.phone, e.title AS exam_title, e.label, s.name AS subject_name
           FROM attempts a
           JOIN users u ON u.id=a.user_id
           JOIN exams e ON e.id=a.exam_id
           LEFT JOIN subjects s ON s.id=e.subject_id
           WHERE a.id=?""",
        (attempt_id,),
    ).fetchone()
    if not attempt:
        conn.close()
        abort(404)

    rows = conn.execute(
        """SELECT q.*, ans.id AS ans_id, ans.chosen_option, ans.text_answer, ans.is_correct
           FROM attempt_answers ans
           JOIN questions q ON q.id=ans.question_id
           WHERE ans.attempt_id=?
           ORDER BY COALESCE(q.q_no, q.id)""",
        (attempt_id,),
    ).fetchall()
    conn.close()
    return render_template("admin/attempt_detail.html", site_name=SITE_NAME, attempt=attempt, rows=rows)

# ---- written review queue (employee/admin will judge)
@app.route("/admin/written")
@admin_required
def admin_written_queue():
    conn = get_db()
    pending = conn.execute(
        """SELECT ans.id AS ans_id, a.id AS attempt_id, u.name AS student_name, u.phone,
                  e.title AS exam_title, q.q_no, q.question, ans.text_answer, q.correct_text
           FROM attempt_answers ans
           JOIN attempts a ON a.id=ans.attempt_id
           JOIN users u ON u.id=a.user_id
           JOIN questions q ON q.id=ans.question_id
           JOIN exams e ON e.id=a.exam_id
           WHERE q.q_type='text' AND ans.is_correct IS NULL
           ORDER BY a.submitted_at DESC"""
    ).fetchall()
    employees = conn.execute("SELECT * FROM employees ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin/written_queue.html", site_name=SITE_NAME, pending=pending, employees=employees)

@app.post("/admin/written/<int:ans_id>/decide")
@admin_required
def admin_written_decide(ans_id):
    decided = request.form.get("decided")  # '1' correct, '0' wrong
    emp_id = request.form.get("reviewer_employee_id") or None
    note = (request.form.get("note") or "").strip() or None
    if decided not in ("0","1"):
        abort(400)
    decided_i = int(decided)
    emp = int(emp_id) if (emp_id and emp_id.isdigit()) else None

    conn = get_db()
    # find attempt + question
    row = conn.execute(
        """SELECT ans.attempt_id, q.id AS qid
           FROM attempt_answers ans
           JOIN questions q ON q.id=ans.question_id
           WHERE ans.id=?""",
        (ans_id,),
    ).fetchone()
    if not row:
        conn.close()
        abort(404)

    # update answer
    conn.execute("UPDATE attempt_answers SET is_correct=? WHERE id=?", (decided_i, ans_id))
    conn.execute(
        """INSERT INTO written_reviews (attempt_answer_id, reviewer_employee_id, decided_correct, note, reviewed_at)
           VALUES (?,?,?,?,?)""",
        (ans_id, emp, decided_i, note, utcnow().isoformat()),
    )

    # update attempt: pending_written -1; score +1 if correct
    att = conn.execute("SELECT score, pending_written FROM attempts WHERE id=?", (row["attempt_id"],)).fetchone()
    new_pending = max(0, int(att["pending_written"]) - 1) if att else 0
    new_score = int(att["score"] or 0) + (1 if decided_i == 1 else 0) if att else (1 if decided_i == 1 else 0)
    conn.execute(
        "UPDATE attempts SET pending_written=?, score=? WHERE id=?",
        (new_pending, new_score, row["attempt_id"]),
    )

    conn.commit()
    conn.close()
    flash("Written answer reviewed ✅", "success")
    return redirect(url_for("admin_written_queue"))

# ---- retake
@app.route("/admin/retake", methods=["GET", "POST"])
@admin_required
def admin_retake():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        exam_id = int(request.form.get("exam_id") or 0)
        extra = int(request.form.get("extra") or 1)

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        exam = conn.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
        if not user or not exam:
            conn.close()
            flash("Student বা Exam পাওয়া যায়নি", "danger")
            return redirect(url_for("admin_retake"))

        last = get_last_attempt(conn, user["id"], exam_id)
        if last is None:
            conn.execute(
                "INSERT INTO attempts (user_id, exam_id, started_at, allowed_extra_attempts) VALUES (?,?,?,?)",
                (user["id"], exam_id, utcnow().isoformat(), extra),
            )
        else:
            conn.execute(
                "UPDATE attempts SET allowed_extra_attempts = allowed_extra_attempts + ? WHERE id=?",
                (extra, last["id"]),
            )
        conn.commit()
        conn.close()
        flash("Retake permission দেওয়া হয়েছে ✅", "success")
        return redirect(url_for("admin_attempts"))

    conn = get_db()
    exams = conn.execute(
        """SELECT e.*, s.name AS subject_name
           FROM exams e LEFT JOIN subjects s ON s.id=e.subject_id
           ORDER BY e.id DESC"""
    ).fetchall()
    conn.close()
    return render_template("admin/retake.html", site_name=SITE_NAME, exams=exams)

# ---- exports
@app.route("/admin/export/attempts.csv")
@admin_required
def export_attempts_csv():
    conn = get_db()
    rows = conn.execute(
        """SELECT a.id AS attempt_id, u.name AS student_name, u.phone, u.whatsapp,
                  s2.name AS student_subject,
                  e.title AS exam_title, e.label, s.name AS subject_name,
                  a.started_at, a.submitted_at, a.score, a.total, a.pending_written
           FROM attempts a
           JOIN users u ON u.id=a.user_id
           LEFT JOIN subjects s2 ON s2.id=u.subject_id
           JOIN exams e ON e.id=a.exam_id
           LEFT JOIN subjects s ON s.id=e.subject_id
           WHERE a.submitted_at IS NOT NULL
           ORDER BY a.submitted_at DESC"""
    ).fetchall()
    conn.close()

    output = StringIO()
    w = csv.writer(output)
    w.writerow(["attempt_id","student_name","phone","whatsapp","student_subject",
                "exam_subject","exam","panel","started_at","submitted_at","score","total","pending_written"])
    for r in rows:
        w.writerow([r["attempt_id"], r["student_name"], r["phone"], r["whatsapp"], r["student_subject"],
                    r["subject_name"], r["exam_title"], r["label"], r["started_at"], r["submitted_at"],
                    r["score"], r["total"], r["pending_written"]])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=attempts.csv"},
    )

@app.route("/admin/export/students.csv")
@admin_required
def export_students_csv():
    """Export students with optional filters: ?subject_id=&batch_id=&status="""
    subject_id = request.args.get("subject_id")
    batch_id = request.args.get("batch_id")
    status = (request.args.get("status") or "").strip()

    where = []
    params = []

    if subject_id and str(subject_id).isdigit():
        where.append("u.subject_id=?")
        params.append(int(subject_id))
    if batch_id and str(batch_id).isdigit():
        where.append("u.batch_id=?")
        params.append(int(batch_id))
    if status:
        where.append("u.status=?")
        params.append(status)

    wh = ("WHERE " + " AND ".join(where)) if where else ""

    conn = get_db()
    rows = conn.execute(
        f"""SELECT u.id, u.name, u.phone, u.whatsapp, u.status, u.is_blocked,
                  s.name AS subject_name, b.name AS batch_name,
                  u.location, u.current_status, u.education_level, u.created_at
           FROM users u
           LEFT JOIN subjects s ON s.id=u.subject_id
           LEFT JOIN batches b ON b.id=u.batch_id
           {wh}
           ORDER BY u.id DESC""",
        params,
    ).fetchall()
    conn.close()

    output = StringIO()
    w = csv.writer(output)
    w.writerow(["id","name","phone","whatsapp","status","is_blocked","subject","batch",
                "location","current_status","education_level","created_at"])
    for r in rows:
        w.writerow([r["id"], r["name"], r["phone"], r["whatsapp"], r["status"], r["is_blocked"],
                    r["subject_name"], r["batch_name"], r["location"], r["current_status"], r["education_level"], r["created_at"]])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=students.csv"},
    )




@app.route("/courses")
def courses():
    subject_id = request.args.get("subject")
    conn = get_db()
    subjects = conn.execute("SELECT * FROM subjects WHERE is_active=1 ORDER BY id DESC").fetchall()
    if subject_id and str(subject_id).isdigit():
        rows = conn.execute(
            """SELECT c.*, s.name AS subject_name
               FROM courses c LEFT JOIN subjects s ON s.id=c.subject_id
               WHERE c.subject_id=?
               ORDER BY c.id DESC""",
            (int(subject_id),),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT c.*, s.name AS subject_name
               FROM courses c LEFT JOIN subjects s ON s.id=c.subject_id
               ORDER BY c.id DESC"""
        ).fetchall()
    conn.close()
    return render_template("student/courses.html", subjects=subjects, courses=rows)

@app.route("/courses/<int:course_id>")
def course_detail(course_id):
    conn = get_db()
    c = conn.execute(
        """SELECT c.*, s.name AS subject_name
           FROM courses c LEFT JOIN subjects s ON s.id=c.subject_id
           WHERE c.id=?""",
        (course_id,),
    ).fetchone()
    conn.close()
    if not c:
        abort(404)
    return render_template("student/course_detail.html", c=c)

@app.route("/admin/courses", methods=["GET", "POST"])
@staff_required
def manage_courses():
    conn = get_db()
    subjects = conn.execute("SELECT * FROM subjects ORDER BY id DESC").fetchall()

    if request.method == "POST":
        subject_id = request.form.get("subject_id")
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip() or None
        details = request.form.get("details", "").strip() or None
        img = request.files.get("image")
        img_filename = None

        if not title or not subject_id or not str(subject_id).isdigit():
            flash("Title + subject required", "danger")
        else:
            sid = int(subject_id)
            if img and img.filename:
                ext = os.path.splitext(img.filename.lower())[1]
                if ext not in (".png", ".jpg", ".jpeg"):
                    flash("Image must be PNG/JPG", "danger")
                else:
                    up_dir = os.path.join(BASE_DIR, "static", "uploads")
                    os.makedirs(up_dir, exist_ok=True)
                    img_filename = f"course_{int(utcnow().timestamp())}{ext}"
                    img.save(os.path.join(up_dir, img_filename))

            conn.execute(
                """INSERT INTO courses (subject_id, title, description, details, image_filename, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (sid, title, description, details, img_filename, utcnow().isoformat()),
            )
            conn.commit()
            flash("Course added ✅", "success")

    rows = conn.execute(
        """SELECT c.*, s.name AS subject_name
           FROM courses c LEFT JOIN subjects s ON s.id=c.subject_id
           ORDER BY c.id DESC"""
    ).fetchall()
    conn.close()
    return render_template("admin/courses.html", subjects=subjects, courses=rows)

@app.post("/admin/courses/<int:course_id>/delete")
@staff_required
def delete_course(course_id):
    conn = get_db()
    conn.execute("DELETE FROM courses WHERE id=?", (course_id,))
    conn.commit()
    conn.close()
    flash("Course deleted", "success")
    return redirect(url_for("manage_courses"))

# ---------------- main ----------------
if __name__ == "__main__":
    init_db()
    app.run(debug=True)
else:
    init_db()
