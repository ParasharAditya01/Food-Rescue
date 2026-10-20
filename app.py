from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, g, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("CAMPUS_FOOD_DB", BASE_DIR / "database.db"))
# Privileged accounts use email + password configured as environment variables.
# Never hard-code passwords in source code or commit them to GitHub.
ADMIN_EMAIL = os.environ.get("CAMPUS_ADMIN_EMAIL", os.environ.get("ADMIN_EMAIL", "")).strip().lower()
ADMIN_PASSWORD = os.environ.get("CAMPUS_ADMIN_PASSWORD", os.environ.get("ADMIN_PASSWORD", ""))
HOST_EMAIL = os.environ.get("CAMPUS_HOST_EMAIL", os.environ.get("HOST_EMAIL", "")).strip().lower()
HOST_CODE = os.environ.get("CAMPUS_HOST_CODE", os.environ.get("HOST_CODE", ""))

app = Flask(__name__, static_folder=None)
app.config["JSON_SORT_KEYS"] = False

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    location TEXT NOT NULL,
    contact_number TEXT NOT NULL DEFAULT '',
    price_per_portion REAL,
    category TEXT NOT NULL DEFAULT 'Other',
    deadline TEXT NOT NULL,
    emoji TEXT NOT NULL DEFAULT '🍽️',
    status TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available','claimed','expired')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL REFERENCES listings(id),
    user_email TEXT NOT NULL,
    user_name TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved'
);
CREATE INDEX IF NOT EXISTS idx_listings_deadline ON listings(deadline);
CREATE INDEX IF NOT EXISTS idx_reservations_listing ON reservations(listing_id);
CREATE TABLE IF NOT EXISTS admin_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    food_name TEXT NOT NULL,
    student_name TEXT NOT NULL,
    student_email TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_admin_notifications_seen ON admin_notifications(seen_at);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','student')),
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS account_roles (
    email TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('admin','student')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_sessions (
    token TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS menu_items (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    cook_minutes INTEGER NOT NULL CHECK(cook_minutes BETWEEN 1 AND 240),
    portions_per_batch INTEGER NOT NULL DEFAULT 1 CHECK(portions_per_batch BETWEEN 1 AND 99),
    price_per_portion REAL,
    serving_size TEXT NOT NULL DEFAULT '',
    pickup_location TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meal_orders (
    id TEXT PRIMARY KEY,
    menu_item_id TEXT NOT NULL REFERENCES menu_items(id),
    student_email TEXT NOT NULL,
    student_name TEXT NOT NULL,
    ordered_at TEXT NOT NULL,
    ready_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'preparing' CHECK(status IN ('preparing','ready','collected','cancelled')),
    portions INTEGER NOT NULL DEFAULT 1 CHECK(portions BETWEEN 1 AND 99)
);
CREATE INDEX IF NOT EXISTS idx_meal_orders_student ON meal_orders(student_email, ordered_at);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_error: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        # Seed an illustrative menu on first deployment. Stable IDs make this safe
        # to run on every restart without duplicating items or overwriting admin edits.
        sample_menu = [
            ("sample-cafe-cappuccino", "Cappuccino", "Drinks", "120 ml per portion. Espresso with steamed milk and a light foam. Illustrative serving size; confirm with your campus provider.", 7),
            ("sample-cafe-latte", "Caffè Latte", "Drinks", "240 ml per portion. Espresso with steamed milk. Illustrative serving size.", 7),
            ("sample-cafe-americano", "Americano", "Drinks", "240 ml per portion. Espresso diluted with hot water. Illustrative serving size.", 5),
            ("sample-cafe-mocha", "Caffè Mocha", "Drinks", "240 ml per portion. Coffee with chocolate and milk. Illustrative serving size.", 8),
            ("sample-cafe-frappe", "Java Chip-style Blended Coffee", "Drinks", "350 ml per portion. Iced blended coffee with chocolate chips. Illustrative serving size.", 10),
            ("sample-cafe-matcha", "Iced Matcha Latte", "Drinks", "240 ml per portion. Chilled milk with matcha green tea. Illustrative serving size.", 6),
            ("sample-bakery-croissant", "Butter Croissant", "Bakery", "1 piece per portion (approximately 60–80 g). Illustrative serving size.", 5),
            ("sample-bakery-brownie", "Chocolate Brownie", "Sweets & Desserts", "1 piece per portion (approximately 70–90 g). Illustrative serving size.", 5),
            ("sample-taj-paneer-pepper", "Paneer Pepper Salt", "Snacks", "1 serving per portion (approximately 180 g). Illustrative serving size.", 15),
            ("sample-taj-crispy-corn", "Crispy Corn with Ginger & Spring Onion", "Snacks", "1 serving per portion (approximately 180 g). Illustrative serving size.", 15),
            ("sample-taj-chicken-dim-sum", "Steamed Chicken Dim Sum", "Snacks", "4 pieces per portion. Illustrative serving size.", 18),
            ("sample-taj-veg-dim-sum", "Broccoli & Corn Dim Sum", "Snacks", "4 pieces per portion. Illustrative serving size.", 18),
            ("sample-taj-cheese-board", "Artisan Cheese Board", "Snacks", "1 platter per portion (approximately 200 g). Illustrative serving size.", 10),
            ("sample-indian-paneer", "Paneer Butter Masala with Rice", "Lunch", "1 plate per portion (approximately 350 g including rice). Illustrative serving size.", 20),
            ("sample-indian-biryani", "Vegetable Biryani", "Lunch", "1 plate per portion (approximately 300 g). Illustrative serving size.", 20),
            ("sample-indian-dosa", "Masala Dosa", "Breakfast", "1 piece per portion, served with chutney and sambar. Illustrative serving size.", 15),
            ("sample-sandwich", "Grilled Cheese & Tomato Sandwich", "Snacks", "1 sandwich per portion (2 triangular halves). Illustrative serving size.", 10),
            ("sample-salad", "Seasonal Garden Salad", "Lunch", "1 bowl per portion (approximately 200 g). Illustrative serving size.", 8),
        ]
        order_columns = {row[1] for row in conn.execute("PRAGMA table_info(meal_orders)")}
        if "portions" not in order_columns:
            conn.execute("ALTER TABLE meal_orders ADD COLUMN portions INTEGER NOT NULL DEFAULT 1")
        if "approval_status" not in order_columns:
            # Preserve existing orders as approved when upgrading an older database.
            conn.execute("ALTER TABLE meal_orders ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'accepted'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_meal_orders_approval ON meal_orders(approval_status, ordered_at)")
        menu_columns = {row[1] for row in conn.execute("PRAGMA table_info(menu_items)")}
        if "portions_per_batch" not in menu_columns:
            conn.execute("ALTER TABLE menu_items ADD COLUMN portions_per_batch INTEGER NOT NULL DEFAULT 1")
        if "price_per_portion" not in menu_columns:
            conn.execute("ALTER TABLE menu_items ADD COLUMN price_per_portion REAL")
        if "serving_size" not in menu_columns:
            conn.execute("ALTER TABLE menu_items ADD COLUMN serving_size TEXT NOT NULL DEFAULT ''")
        sample_prices = {
            "sample-cafe-cappuccino": 220, "sample-cafe-latte": 240,
            "sample-cafe-americano": 200, "sample-cafe-mocha": 280,
            "sample-cafe-frappe": 320, "sample-cafe-matcha": 280,
            "sample-bakery-croissant": 160, "sample-bakery-brownie": 150,
            "sample-taj-paneer-pepper": 350, "sample-taj-crispy-corn": 350,
            "sample-taj-chicken-dim-sum": 400, "sample-taj-veg-dim-sum": 350,
            "sample-taj-cheese-board": 450, "sample-indian-paneer": 220,
            "sample-indian-biryani": 180, "sample-indian-dosa": 100,
            "sample-sandwich": 140, "sample-salad": 150,
        }
        for item_id, name, category, description, cook_minutes in sample_menu:
            conn.execute(
                "INSERT OR IGNORE INTO menu_items(id,name,category,description,cook_minutes,price_per_portion,pickup_location,active,created_at,created_by) "
                "VALUES(?,?,?,?,?,?,'Campus Café Demo Counter',1,?,'sample-menu')",
                (item_id, name, category, description, cook_minutes, sample_prices[item_id], iso_now()),
            )
        sample_servings = {
            "sample-cafe-cappuccino": "120 ml", "sample-cafe-latte": "240 ml",
            "sample-cafe-americano": "240 ml", "sample-cafe-mocha": "240 ml",
            "sample-cafe-frappe": "350 ml", "sample-cafe-matcha": "240 ml",
            "sample-bakery-croissant": "1 piece (60–80 g)", "sample-bakery-brownie": "1 piece (70–90 g)",
            "sample-taj-paneer-pepper": "1 serving (approx. 180 g)", "sample-taj-crispy-corn": "1 serving (approx. 180 g)",
            "sample-taj-chicken-dim-sum": "4 pieces", "sample-taj-veg-dim-sum": "4 pieces",
            "sample-taj-cheese-board": "1 platter (approx. 200 g)", "sample-indian-paneer": "1 plate (approx. 350 g)",
            "sample-indian-biryani": "1 plate (approx. 300 g)", "sample-indian-dosa": "1 piece",
            "sample-sandwich": "1 sandwich (2 triangular halves)", "sample-salad": "1 bowl (approx. 200 g)",
        }
        # Keep illustrative prices and portion sizes stable without touching admin-created items.
        sample_batch_capacity = {
            "sample-cafe-cappuccino": 2, "sample-cafe-latte": 2, "sample-cafe-americano": 3,
            "sample-cafe-mocha": 2, "sample-cafe-frappe": 2, "sample-cafe-matcha": 2,
            "sample-bakery-croissant": 6, "sample-bakery-brownie": 6,
            "sample-taj-paneer-pepper": 3, "sample-taj-crispy-corn": 3,
            "sample-taj-chicken-dim-sum": 4, "sample-taj-veg-dim-sum": 4,
            "sample-taj-cheese-board": 2, "sample-indian-paneer": 4,
            "sample-indian-biryani": 6, "sample-indian-dosa": 2,
            "sample-sandwich": 2, "sample-salad": 3,
        }
        for item_id, price in sample_prices.items():
            conn.execute("UPDATE menu_items SET price_per_portion=?, serving_size=?, portions_per_batch=? WHERE id=? AND created_by='sample-menu'", (price, sample_servings[item_id], sample_batch_capacity[item_id], item_id))
        # Safely upgrade databases created before contact/price fields existed.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
        if "contact_number" not in columns:
            conn.execute("ALTER TABLE listings ADD COLUMN contact_number TEXT NOT NULL DEFAULT ''")
        if "price_per_portion" not in columns:
            conn.execute("ALTER TABLE listings ADD COLUMN price_per_portion REAL")
        if "category" not in columns:
            conn.execute("ALTER TABLE listings ADD COLUMN category TEXT NOT NULL DEFAULT 'Other'")
        session_columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "last_seen" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN last_seen TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE sessions SET last_seen = created_at WHERE last_seen = ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_last_seen ON sessions(last_seen)")
        # Preserve account roles even after a user signs out and their session row is deleted.
        conn.execute(
            """
            INSERT OR IGNORE INTO account_roles(email, role, created_at)
            SELECT lower(email), MIN(role), MIN(created_at)
            FROM sessions
            WHERE lower(email) NOT IN (?, ?)
            GROUP BY lower(email)
            HAVING COUNT(DISTINCT role) = 1
            """,
            (HOST_EMAIL or "__no_host_configured__", ADMIN_EMAIL or "__no_admin_configured__"),
        )

        # Do not purge registered accounts during startup or deployment.
        conn.commit()
    finally:
        conn.close()


def expire_listings(db: sqlite3.Connection) -> None:
    # Once the collection deadline passes, the listing remains visible as Expired
    # for 24 hours, but it can no longer be reserved and has no available portions.
    db.execute(
        "UPDATE listings SET quantity = 0, status = 'expired' "
        "WHERE deadline <= ? AND status != 'expired'",
        (iso_now(),),
    )


def listing_to_dict(row: sqlite3.Row, db: sqlite3.Connection) -> dict[str, Any]:
    reservation_count = db.execute(
        "SELECT COUNT(*) FROM reservations WHERE listing_id = ?", (row["id"],)
    ).fetchone()[0]
    # Public listings show the publisher's display name, never their email.
    creator = db.execute(
        "SELECT name FROM sessions WHERE lower(email) = lower(?) ORDER BY created_at DESC LIMIT 1",
        (row["created_by"],),
    ).fetchone()
    created_by_display = "Host" if row["created_by"].strip().lower() == HOST_EMAIL and HOST_EMAIL else (
        creator["name"] if creator else "Campus Admin"
    )
    return {
        "id": row["id"], "name": row["name"], "quantity": row["quantity"],
        "location": row["location"],
        "contactNumber": row["contact_number"],
        "pricePerPortion": row["price_per_portion"],
        "category": row["category"] if "category" in row.keys() else "Other",
        "deadline": row["deadline"],
        "emoji": row["emoji"], "status": row["status"],
        "createdAt": row["created_at"], "createdBy": created_by_display,
        "reservationCount": reservation_count,
    }


def error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def current_session(required_role: str | None = None):
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        return None, error("Register first to continue.", 401)
    db = get_db()
    session = db.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
    if session is None:
        return None, error("Your session is no longer valid. Register again.", 401)
    db.execute("UPDATE sessions SET last_seen = ? WHERE token = ?", (iso_now(), token))
    if required_role and session["role"] != required_role:
        return None, error(f"Only {required_role}s can perform this action.", 403)
    return session, None


def current_host():
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        return None, error("Host login required.", 401)
    db = get_db()
    session = db.execute("SELECT * FROM host_sessions WHERE token = ?", (token,)).fetchone()
    if session is None or session["email"].lower() != HOST_EMAIL:
        return None, error("Host session is invalid. Sign in again.", 401)
    db.execute("UPDATE host_sessions SET last_seen = ? WHERE token = ?", (iso_now(), token))
    return session, None


@app.get("/robots.txt")
def robots_txt():
    body = "User-agent: *\\nAllow: /\\nDisallow: /api/\\nSitemap: https://food-rescue-p920.onrender.com/sitemap.xml\\n"
    return app.response_class(body, mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    paths = ["/", "/#food-categories", "/#listings", "/#meal-orders", "/#how-it-works", "/#safety", "/#faq", "/#privacy-policy"]
    entries = "".join(f"<url><loc>https://food-rescue-p920.onrender.com{path}</loc></url>" for path in paths)
    body = '<?xml version="1.0" encoding="UTF-8"?>' + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + entries + '</urlset>'
    return app.response_class(body, mimetype="application/xml")


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "database": "sqlite", "time": iso_now()})


@app.post("/api/session")
def create_session():
    """Register a new account once, or sign in to an existing account."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    # Remove invisible formatting characters often introduced by copy/paste.
    email = str(data.get("email", "")).translate({ord(ch): None for ch in "\u200b\u200c\u200d\ufeff"}).strip().lower()
    role = str(data.get("role", "student")).strip().lower()
    password = str(data.get("password", ""))
    action = str(data.get("action", "signup")).strip().lower()

    if action not in ("signup", "login"):
        return error("Choose Sign up or Log in.")
    if action == "signup" and (len(name) < 2 or len(name) > 100):
        return error("Enter a name between 2 and 100 characters.")
    if action == "login" and not name:
        name = email.split("@", 1)[0] if "@" in email else ""
    if len(email) > 254 or not __import__("re").fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return error("Email format was not recognized. Remove spaces and enter the complete address, such as name@example.com.")
    if role not in ("student", "admin"):
        return error("Choose student or admin.")
    if HOST_EMAIL and secrets.compare_digest(email, HOST_EMAIL):
        return error("That email is reserved for Host access. Use the Host login.")

    db = get_db()
    account = db.execute("SELECT role FROM account_roles WHERE email = ?", (email,)).fetchone()

    # Recover legacy registrations from sessions if the account registry is missing a row.
    if account is None:
        old_roles = {
            row["role"] for row in db.execute(
                "SELECT DISTINCT role FROM sessions WHERE lower(email) = ?", (email,)
            ).fetchall()
        }
        if len(old_roles) == 1:
            old_role = next(iter(old_roles))
            db.execute(
                "INSERT OR IGNORE INTO account_roles(email,role,created_at) VALUES(?,?,?)",
                (email, old_role, iso_now()),
            )
            account = db.execute("SELECT role FROM account_roles WHERE email = ?", (email,)).fetchone()

    if action == "signup" and account is not None:
        return error("This email is already registered. Please choose Log in instead of Sign up.", 409)

    if action == "login" and account is None:
        return error("No account exists for this email yet. Please choose Sign up first.", 404)

    if role == "admin":
        if not ADMIN_PASSWORD:
            return error("Admin sign-in is not configured. Set CAMPUS_ADMIN_PASSWORD in the backend environment.", 503)
        configured_admin = bool(ADMIN_EMAIL and secrets.compare_digest(email, ADMIN_EMAIL))
        host_authorized_admin = bool(account and account["role"] == "admin")
        if not configured_admin and not host_authorized_admin:
            return error("This email has not been authorized as Admin by the Host.", 403)
        if not secrets.compare_digest(password, ADMIN_PASSWORD):
            return error("Admin email or password is incorrect.", 403)
    elif ADMIN_EMAIL and secrets.compare_digest(email, ADMIN_EMAIL):
        return error("This email is reserved for Admin sign-in. Choose Admin and enter the admin password.", 403)

    if account is not None and account["role"] != role:
        return error(
            "This email is registered with a different role. Contact the Host if the role needs to be changed.",
            403,
        )

    if action == "signup":
        db.execute(
            "INSERT INTO account_roles(email,role,created_at) VALUES(?,?,?)",
            (email, role, iso_now()),
        )

    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO sessions(token,name,email,role,created_at,last_seen) VALUES(?,?,?,?,?,?)",
        (token, name, email, role, iso_now(), iso_now()),
    )
    return jsonify({"token": token, "user": {"name": name, "email": email, "role": role}}), 201


@app.post("/api/host/session")
def create_host_session():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if not HOST_EMAIL or not HOST_CODE:
        return error("Host sign-in is not configured. Set CAMPUS_HOST_EMAIL and CAMPUS_HOST_CODE in the backend environment.", 503)
    if not secrets.compare_digest(email, HOST_EMAIL) or not secrets.compare_digest(password, HOST_CODE):
        return error("Host email or password is incorrect.", 403)
    token = secrets.token_urlsafe(40)
    now = iso_now()
    get_db().execute(
        "INSERT INTO host_sessions(token,email,created_at,last_seen) VALUES(?,?,?,?)",
        (token, HOST_EMAIL, now, now),
    )
    return jsonify({"token": token, "host": {"name": "Host", "role": "host"}}), 201


@app.get("/api/host/overview")
def host_overview():
    host, failure = current_host()
    if failure:
        return failure
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        expire_listings(db)
        listings = db.execute("SELECT * FROM listings ORDER BY created_at DESC").fetchall()
        users = db.execute(
            "SELECT name,email,role,created_at,last_seen FROM sessions "
            "WHERE lower(email) != ? ORDER BY last_seen DESC",
            (HOST_EMAIL,),
        ).fetchall()
        reservations = db.execute(
            "SELECT r.id,r.listing_id,r.user_email,r.user_name,r.reserved_at,r.status,l.name AS food_name "
            "FROM reservations r LEFT JOIN listings l ON l.id=r.listing_id ORDER BY r.id DESC LIMIT 500"
        ).fetchall()
        result = {
            "users": [{
                "name": u["name"], "email": u["email"], "role": u["role"],
                "createdAt": u["created_at"], "lastSeen": u["last_seen"],
                "active": bool(u["last_seen"] and (utc_now() - datetime.fromisoformat(u["last_seen"])).total_seconds() <= 180)
            } for u in users],
            "listings": [
                listing_to_dict(row, db) | {
                    "createdBy": (
                        "Host" if HOST_EMAIL and row["created_by"].strip().lower() == HOST_EMAIL
                        else row["created_by"]
                    )
                }
                for row in listings
            ],
            "reservations": [{
                "id": r["id"], "listingId": r["listing_id"], "foodName": r["food_name"] or "(deleted listing)",
                "studentName": r["user_name"],
                "studentEmail": "" if r["user_email"].strip().lower() == HOST_EMAIL else r["user_email"],
                "reservedAt": r["reserved_at"], "status": r["status"]
            } for r in reservations],
            "summary": {
                "users": len(users),
                "activeUsers": sum(1 for u in users if u["last_seen"] and (utc_now() - datetime.fromisoformat(u["last_seen"])).total_seconds() <= 180),
                "admins": sum(1 for u in users if u["role"] == "admin"),
                "students": sum(1 for u in users if u["role"] == "student"),
                "listings": len(listings),
                "reservations": db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
            }
        }
        db.commit()
        return jsonify(result)
    except Exception:
        db.rollback()
        app.logger.exception("Host overview failed")
        return error("Could not load the Host dashboard.", 500)


@app.patch("/api/host/users/role")
def host_change_role():
    host, failure = current_host()
    if failure:
        return failure
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    role = str(data.get("role", "")).strip().lower()
    if role not in ("admin", "student"):
        return error("Role must be admin or student.")
    if not email or email == HOST_EMAIL:
        return error("That account cannot be changed.")
    db = get_db()
    known_account = db.execute(
        "SELECT email FROM account_roles WHERE email = ? UNION SELECT email FROM sessions WHERE lower(email) = ? LIMIT 1",
        (email, email),
    ).fetchone()
    if known_account is None:
        return error("No account was found for that email.", 404)
    db.execute(
        "INSERT INTO account_roles(email,role,created_at) VALUES(?,?,?) "
        "ON CONFLICT(email) DO UPDATE SET role=excluded.role",
        (email, role, iso_now()),
    )
    cursor = db.execute("UPDATE sessions SET role = ? WHERE lower(email) = ?", (role, email))
    return jsonify({"ok": True, "updatedSessions": cursor.rowcount, "email": email, "role": role})


@app.delete("/api/host/users/sessions")
def host_revoke_sessions():
    host, failure = current_host()
    if failure:
        return failure
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    if not email or email == HOST_EMAIL:
        return error("That account cannot be signed out by this action.")
    cursor = get_db().execute("DELETE FROM sessions WHERE lower(email) = ?", (email,))
    return jsonify({"ok": True, "revokedSessions": cursor.rowcount})


@app.delete("/api/host/listings/<listing_id>")
def host_delete_listing(listing_id: str):
    host, failure = current_host()
    if failure:
        return failure
    db = get_db()
    exists = db.execute("SELECT id FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if exists is None:
        return error("Listing not found.", 404)
    # Preserve reservation history; listing rows are retained, but hidden from public browsing.
    db.execute("UPDATE listings SET quantity=0,status='claimed' WHERE id=?", (listing_id,))
    return jsonify({"ok": True, "message": "Listing closed and removed from availability."})


@app.delete("/api/host/session")
def delete_host_session():
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if token:
        get_db().execute("DELETE FROM host_sessions WHERE token = ?", (token,))
    return jsonify({"ok": True})


@app.get("/api/admin/notifications")
def get_admin_notifications():
    session, failure = current_session("admin")
    if failure:
        return failure
    db = get_db()
    rows = db.execute(
        "SELECT id, listing_id, food_name, student_name, student_email, reserved_at, seen_at "
        "FROM admin_notifications ORDER BY id DESC LIMIT 100"
    ).fetchall()
    notifications = [{
        "id": row["id"], "listingId": row["listing_id"], "foodName": row["food_name"],
        "studentName": row["student_name"],
        "reservedAt": row["reserved_at"], "isNew": row["seen_at"] is None
    } for row in rows]
    new_ids = [row["id"] for row in rows if row["seen_at"] is None]
    if new_ids:
        placeholders = ",".join("?" for _ in new_ids)
        db.execute(
            f"UPDATE admin_notifications SET seen_at = ? WHERE id IN ({placeholders})",
            [iso_now(), *new_ids],
        )
    return jsonify({"notifications": notifications, "newCount": len(new_ids)})


@app.get("/api/session")
def get_session():
    session, failure = current_session()
    if failure:
        return failure
    return jsonify({"user": {
        "name": session["name"],
        "email": "" if session["email"].strip().lower() == HOST_EMAIL else session["email"],
        "role": session["role"]
    }})


@app.delete("/api/session")
def delete_session():
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if token:
        get_db().execute("DELETE FROM sessions WHERE token = ?", (token,))
    return jsonify({"ok": True})


@app.get("/api/listings")
def get_listings():
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        expire_listings(db)
        retention_cutoff = (utc_now().timestamp() - 24 * 60 * 60)
        cutoff_dt = datetime.fromtimestamp(retention_cutoff, timezone.utc).isoformat(timespec="seconds")
        rows = db.execute(
            "SELECT * FROM listings "
            "WHERE julianday(CASE WHEN deadline > created_at THEN deadline ELSE created_at END) >= julianday(?) "
            "ORDER BY deadline ASC",
            (cutoff_dt,),
        ).fetchall()
        result = [listing_to_dict(row, db) for row in rows]
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"listings": result})


@app.post("/api/listings")
def create_listing():
    session, failure = current_session("admin")
    if failure:
        return failure
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    location = str(data.get("location", "")).strip()
    contact_number = str(data.get("contactNumber", "")).strip()
    raw_price = data.get("pricePerPortion")
    category = str(data.get("category", "Other")).strip()
    allowed_categories = {"Breakfast", "Lunch", "Dinner", "Snacks", "Sweets & Desserts", "Drinks", "Bakery", "Fruits", "Other"}
    if category not in allowed_categories:
        return error("Choose a valid food category.")
    emoji = str(data.get("emoji", "🍽️")).strip()[:16] or "🍽️"
    quantity = data.get("quantity")
    deadline_raw = str(data.get("deadline", "")).strip()
    if not name or len(name) > 120:
        return error("Food name is required and must be at most 120 characters.")
    if not location or len(location) > 100:
        return error("Pickup location is required and must be at most 100 characters.")
    if len(contact_number) > 25 or (contact_number and not __import__("re").fullmatch(r"[+0-9() .-]{7,25}", contact_number)):
        return error("Enter a valid contact number (7–25 characters), or leave it blank.")
    price_per_portion = None
    if raw_price is not None and str(raw_price).strip() != "":
        if isinstance(raw_price, bool):
            return error("Price must be a non-negative amount up to ₹1,000,000 per portion.")
        try:
            price_per_portion = float(raw_price)
        except (TypeError, ValueError):
            return error("Enter a valid price per portion, or leave it blank.")
        import math
        if not math.isfinite(price_per_portion) or not 0 <= price_per_portion <= 1_000_000 or round(price_per_portion, 2) != price_per_portion:
            return error("Price must be ₹0–₹1,000,000 per portion, with up to 2 decimal places.")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 500:
        return error("Enter a whole number of portions between 1 and 500.")
    try:
        deadline = datetime.fromisoformat(deadline_raw.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.astimezone()
        deadline = deadline.astimezone(timezone.utc)
    except ValueError:
        return error("Provide a valid collection deadline.")
    if deadline <= utc_now():
        return error("Choose a collection deadline in the future.")
    listing_id = secrets.token_urlsafe(12)
    db = get_db()
    db.execute(
        "INSERT INTO listings(id,name,quantity,location,contact_number,price_per_portion,category,deadline,emoji,status,created_by,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?, 'available',?,?)",
        (listing_id, name, quantity, location, contact_number, price_per_portion, category,
         deadline.isoformat(timespec="seconds"), emoji, session["email"], iso_now()),
    )
    row = db.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    return jsonify({"listing": listing_to_dict(row, db)}), 201


@app.post("/api/listings/<listing_id>/reserve")
def reserve_portion(listing_id: str):
    session, failure = current_session("student")
    if failure:
        return failure
    data = request.get_json(silent=True) or {}
    confirmation_email = str(data.get("confirmationEmail", "")).strip().lower()
    if not confirmation_email or not __import__("re").fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", confirmation_email):
        return error("Enter a valid confirmation email address.")
    if not secrets.compare_digest(confirmation_email, session["email"].strip().lower()):
        return error("The confirmation email does not match the email you registered with. No portion was reserved.", 403)
    db = get_db()
    try:
        # Serialize check-and-decrement so concurrent requests cannot oversell the last portion.
        db.execute("BEGIN IMMEDIATE")
        expire_listings(db)
        row = db.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if row is None:
            db.rollback()
            return error("This food listing no longer exists.", 404)
        if row["status"] == "expired" or row["deadline"] <= iso_now():
            db.execute("UPDATE listings SET quantity=0,status='expired' WHERE id=?", (listing_id,))
            db.commit()
            return error("The collection deadline has passed. This listing is unavailable.", 409)
        if row["quantity"] < 1 or row["status"] == "claimed":
            db.rollback()
            return error("No portions remain for this listing.", 409)
        remaining = row["quantity"] - 1
        status = "claimed" if remaining == 0 else "available"
        db.execute("UPDATE listings SET quantity=?,status=? WHERE id=?", (remaining, status, listing_id))
        reserved_at = iso_now()
        db.execute(
            "INSERT INTO reservations(listing_id,user_email,user_name,reserved_at,status) VALUES(?,?,?,?, 'reserved')",
            (listing_id, session["email"], session["name"], reserved_at),
        )
        db.execute(
            "INSERT INTO admin_notifications(listing_id,food_name,student_name,student_email,reserved_at) "
            "VALUES(?,?,?,?,?)",
            (listing_id, row["name"], session["name"], session["email"], reserved_at),
        )
        db.commit()
        updated = db.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        return jsonify({"listing": listing_to_dict(updated, db), "remaining": remaining}), 200
    except sqlite3.Error:
        if db.in_transaction:
            db.rollback()
        app.logger.exception("Reservation transaction failed")
        return error("The reservation could not be saved. Please try again.", 500)



@app.get("/api/menu")
def get_menu():
    rows = get_db().execute("SELECT * FROM menu_items WHERE active=1 ORDER BY category,name").fetchall()
    return jsonify({"items": [dict(id=r["id"], name=r["name"], category=r["category"], description=r["description"], cookMinutes=r["cook_minutes"], portionsPerBatch=r["portions_per_batch"], pricePerPortion=r["price_per_portion"], servingSize=r["serving_size"], pickupLocation=r["pickup_location"]) for r in rows]})


@app.post("/api/menu")
def create_menu_item():
    session, failure = current_session("admin")
    if failure: return failure
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    category = str(data.get("category", "")).strip()
    description = str(data.get("description", "")).strip()[:300]
    location = str(data.get("pickupLocation", "")).strip()
    try: minutes = int(data.get("cookMinutes"))
    except (TypeError, ValueError): return error("Enter cooking time in minutes.")
    serving_size = str(data.get("servingSize", "")).strip()
    if not serving_size or len(serving_size) > 80: return error("Enter the portion size, such as 120 ml or 1 piece.")
    raw_price = data.get("pricePerPortion")
    try:
        price = float(raw_price)
    except (TypeError, ValueError):
        return error("Enter a valid price per portion.")
    if not math.isfinite(price) or not 0 <= price <= 1_000_000 or round(price, 2) != price:
        return error("Price must be ₹0–₹1,000,000 per portion, with up to 2 decimal places.")
    allowed = {"Breakfast", "Lunch", "Dinner", "Snacks", "Sweets & Desserts", "Drinks", "Bakery", "Fruits", "Other"}
    if not name or len(name)>120: return error("Menu item name is required (maximum 120 characters).")
    if category not in allowed: return error("Choose a valid menu category.")
    if not 1 <= minutes <= 240: return error("Cooking time must be 1–240 minutes.")
    if not location or len(location)>160: return error("Enter a pickup location.")
    item_id = secrets.token_urlsafe(12)
    portions_per_batch = data.get("portionsPerBatch", 1)
    if isinstance(portions_per_batch, bool) or not isinstance(portions_per_batch, int) or not 1 <= portions_per_batch <= 99:
        return error("Set portions per batch to a whole number from 1 to 99.")
    get_db().execute("INSERT INTO menu_items(id,name,category,description,cook_minutes,portions_per_batch,price_per_portion,serving_size,pickup_location,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (item_id,name,category,description,minutes,portions_per_batch,price,serving_size,location,iso_now(),session["email"]))
    return jsonify({"ok":True,"id":item_id}), 201


@app.post("/api/menu/<item_id>/order")
def order_menu_item(item_id: str):
    session, failure = current_session("student")
    if failure: return failure
    data = request.get_json(silent=True) or {}
    portions = data.get("portions", 1)
    if isinstance(portions, bool) or not isinstance(portions, int) or not 1 <= portions <= 99:
        return error("Choose a whole number of portions from 1 to 99.")
    db = get_db()
    row = db.execute("SELECT * FROM menu_items WHERE id=? AND active=1", (item_id,)).fetchone()
    if row is None: return error("This menu item is no longer available.", 404)
    now = utc_now()
    now_text = now.isoformat(timespec="seconds")
    # The cooking timer starts only after an Admin accepts the order.
    existing = db.execute(
        "SELECT * FROM meal_orders WHERE menu_item_id=? AND student_email=? AND approval_status='pending' ORDER BY ordered_at DESC LIMIT 1",
        (item_id, session["email"]),
    ).fetchone()
    total_portions = portions + (existing["portions"] if existing else 0)
    if existing:
        order_id = existing["id"]
        db.execute("UPDATE meal_orders SET portions=?, ordered_at=?, ready_at='', status='preparing' WHERE id=? AND approval_status='pending'", (total_portions, now_text, order_id))
    else:
        order_id = secrets.token_urlsafe(12)
        db.execute("INSERT INTO meal_orders(id,menu_item_id,student_email,student_name,ordered_at,ready_at,status,portions,approval_status) VALUES(?,?,?,?,?,'','preparing',?,'pending')", (order_id,item_id,session["email"],session["name"],now_text,total_portions))
    return jsonify({"ok":True,"order":{"id":order_id,"itemName":row["name"],"pickupLocation":row["pickup_location"],"orderedAt":now_text,"readyAt":"","status":"pending","approvalStatus":"pending","portions":total_portions}}), 201


@app.get("/api/orders")
def get_my_orders():
    session, failure = current_session("student")
    if failure: return failure
    db = get_db()
    now = iso_now()
    db.execute("UPDATE meal_orders SET status='ready' WHERE student_email=? AND approval_status='accepted' AND status='preparing' AND ready_at<>'' AND julianday(ready_at)<=julianday(?)", (session["email"],now))
    rows = db.execute("SELECT o.*,m.name AS item_name,m.pickup_location FROM meal_orders o JOIN menu_items m ON m.id=o.menu_item_id WHERE o.student_email=? ORDER BY o.ordered_at DESC LIMIT 100", (session["email"],)).fetchall()
    return jsonify({"orders":[{"id":r["id"],"itemName":r["item_name"],"menuItemId":r["menu_item_id"],"pickupLocation":r["pickup_location"],"orderedAt":r["ordered_at"],"readyAt":r["ready_at"],"status":r["status"],"approvalStatus":r["approval_status"],"portions":r["portions"]} for r in rows]})


@app.get("/api/admin/orders")
def get_admin_orders():
    session, failure = current_session("admin")
    if failure: return failure
    db = get_db()
    now = iso_now()
    db.execute("UPDATE meal_orders SET status='ready' WHERE approval_status='accepted' AND status='preparing' AND ready_at<>'' AND julianday(ready_at)<=julianday(?)", (now,))
    rows = db.execute("SELECT o.*,m.name AS item_name,m.pickup_location FROM meal_orders o JOIN menu_items m ON m.id=o.menu_item_id ORDER BY CASE o.approval_status WHEN 'pending' THEN 0 ELSE 1 END, o.ordered_at DESC LIMIT 200").fetchall()
    return jsonify({"orders":[{"id":r["id"],"itemName":r["item_name"],"studentName":r["student_name"],"studentEmail":r["student_email"],"menuItemId":r["menu_item_id"],"pickupLocation":r["pickup_location"],"orderedAt":r["ordered_at"],"readyAt":r["ready_at"],"status":r["status"],"approvalStatus":r["approval_status"],"portions":r["portions"]} for r in rows],"pendingCount":sum(1 for r in rows if r["approval_status"]=="pending")})


@app.post("/api/admin/orders/<order_id>/<decision>")
def decide_meal_order(order_id: str, decision: str):
    session, failure = current_session("admin")
    if failure: return failure
    if decision not in ("accept", "reject"): return error("Choose accept or reject.")
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT o.*,m.cook_minutes,m.portions_per_batch FROM meal_orders o JOIN menu_items m ON m.id=o.menu_item_id WHERE o.id=?", (order_id,)).fetchone()
        if row is None:
            db.rollback()
            return error("Order not found.",404)
        if row["approval_status"] != "pending":
            db.rollback()
            return error("This order has already been reviewed.",409)
        if decision == "accept":
            batches = (row["portions"] + row["portions_per_batch"] - 1) // row["portions_per_batch"]
            ready = utc_now().timestamp() + batches * row["cook_minutes"] * 60
            ready_at = datetime.fromtimestamp(ready, timezone.utc).isoformat(timespec="seconds")
            db.execute("UPDATE meal_orders SET approval_status='accepted',status='preparing',ordered_at=?,ready_at=? WHERE id=? AND approval_status='pending'", (iso_now(),ready_at,order_id))
        else:
            db.execute("UPDATE meal_orders SET approval_status='rejected',status='cancelled',ready_at='' WHERE id=? AND approval_status='pending'", (order_id,))
        db.commit()
        return jsonify({"ok":True,"decision":decision,"message":"Order accepted. Cooking timer started." if decision=="accept" else "Order rejected."})
    except sqlite3.Error:
        if db.in_transaction: db.rollback()
        app.logger.exception("Admin order decision failed")
        return error("Could not update this order. Please try again.",500)


@app.post("/api/orders/<order_id>/collected")
def mark_order_collected(order_id: str):
    session, failure = current_session("student")
    if failure: return failure
    db=get_db(); now=iso_now()
    row=db.execute("SELECT * FROM meal_orders WHERE id=? AND student_email=?",(order_id,session["email"])).fetchone()
    if row is None: return error("Order not found.",404)
    if row["approval_status"] != "accepted": return error("Only an accepted order can be collected.",409)
    if row["status"] == "preparing" and (not row["ready_at"] or row["ready_at"] > now): return error("Your food is still being prepared.",409)
    if row["status"] not in ("ready","preparing"): return error("This order cannot be marked as collected.",409)
    db.execute("UPDATE meal_orders SET status='collected' WHERE id=? AND student_email=?",(order_id,session["email"]))
    return jsonify({"ok":True})

@app.errorhandler(404)
def not_found(_error):
    # Keep API errors machine-readable for the frontend; show a designed page for browser routes.
    if request.path.startswith("/api/"):
        return jsonify({"error": "API endpoint not found."}), 404
    page = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#102b22">
  <title>Page not found | Campus Food Rescue</title>
  <style>
    :root{color-scheme:dark;--forest:#102b22;--deep:#091b16;--ivory:#f5f0e5;--muted:#c5cfc7;--gold:#d6b56d}
    *{box-sizing:border-box}
    body{margin:0;min-height:100vh;display:grid;place-items:center;padding:28px;background:radial-gradient(ellipse at 50% 0%,#234638 0%,var(--forest) 46%,var(--deep) 100%);color:var(--ivory);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    main{width:min(100%,720px);text-align:center;padding:clamp(30px,7vw,76px) clamp(20px,5vw,56px);border:1px solid rgba(214,181,109,.38);border-radius:28px;background:rgba(7,24,18,.48);box-shadow:0 28px 90px rgba(0,0,0,.28);position:relative;overflow:hidden}
    main:before{content:"";position:absolute;inset:10px;border:1px solid rgba(245,240,229,.08);border-radius:20px;pointer-events:none}
    .eyebrow{font-size:11px;letter-spacing:.28em;text-transform:uppercase;color:var(--gold);font-weight:700}
    .number{font-family:Georgia,"Times New Roman",serif;font-size:clamp(100px,24vw,190px);line-height:.95;letter-spacing:-.08em;margin:22px 0 18px;color:var(--ivory);text-shadow:0 8px 45px rgba(214,181,109,.12)}
    h1{font-family:Georgia,"Times New Roman",serif;font-weight:400;font-size:clamp(28px,5vw,42px);margin:0 0 14px}
    p{max-width:440px;margin:0 auto;color:var(--muted);font-size:15px;line-height:1.8}
    .actions{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-top:30px}
    a,button{min-height:48px;padding:0 22px;border-radius:999px;font:600 13px inherit;letter-spacing:.02em;display:inline-flex;align-items:center;justify-content:center;text-decoration:none;cursor:pointer;transition:transform .2s,background .2s,border-color .2s}
    a:hover,button:hover{transform:translateY(-2px)}
    .primary{background:var(--gold);color:#14271e;border:1px solid var(--gold)}
    .secondary{background:transparent;color:var(--ivory);border:1px solid rgba(245,240,229,.3)}
    .foot{margin-top:34px;font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:#93a79a}
    @media(max-width:420px){main{border-radius:22px}.actions{flex-direction:column}a,button{width:100%}}
    @media(prefers-reduced-motion:reduce){*,*:before,*:after{transition:none!important}}
  </style>
</head>
<body>
  <main>
    <div class="eyebrow">Campus Food Rescue · Lost in the network</div>
    <div class="number" aria-label="404">404</div>
    <h1>This page isn't on the menu.</h1>
    <p>The page may have moved, or the address may be incorrect. Let's get you back to discovering good food and reducing waste.</p>
    <div class="actions">
      <a class="primary" href="/">Back to homepage <span aria-hidden="true">&nbsp;↗</span></a>
      <button class="secondary" type="button" onclick="if(history.length>1){history.back()}else{location.href='/'}">Go back</button>
    </div>
    <div class="foot">Good food. Good vibes. Less waste.</div>
  </main>
</body>
</html>"""
    return page, 404


@app.errorhandler(500)
def server_error(_error):
    return jsonify({"error": "Unexpected server error. Check the Flask terminal for details."}), 500


init_db()

if __name__ == "__main__":
    # Local development server. Do not expose the Flask debugger to the public internet.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
