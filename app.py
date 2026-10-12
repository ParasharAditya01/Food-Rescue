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
# Change this before demoing publicly. Set CAMPUS_ADMIN_CODE in your environment.
ADMIN_ACCESS_CODE = os.environ.get("CAMPUS_ADMIN_CODE", "ONLY-ADMIN")
HOST_ACCESS_CODE = os.environ.get("CAMPUS_HOST_CODE", "")
HOST_EMAIL = os.environ.get("CAMPUS_HOST_EMAIL", "").strip().lower()

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
            WHERE lower(email) != ?
            GROUP BY lower(email)
            HAVING COUNT(DISTINCT role) = 1
            """,
            (HOST_EMAIL,),
        )
        conn.commit()
    finally:
        conn.close()


def expire_listings(db: sqlite3.Connection) -> None:
    # Expiration atomically removes all remaining portions from availability.
    db.execute(
        "UPDATE listings SET quantity = 0, status = 'expired' "
        "WHERE deadline <= ? AND status = 'available' AND quantity > 0",
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


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "database": "sqlite", "time": iso_now()})


@app.post("/api/session")
def create_session():
    """Create a local prototype session without sending or verifying email."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    role = str(data.get("role", "student")).strip().lower()
    code = str(data.get("adminCode", ""))

    if len(name) < 2 or len(name) > 100:
        return error("Enter a name between 2 and 100 characters.")
    if len(email) > 254 or not __import__("re").fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return error("Enter a valid email address.")
    if HOST_EMAIL and secrets.compare_digest(email, HOST_EMAIL):
        return error("That email is reserved for Host access.")
    if role not in ("student", "admin"):
        return error("Choose student or admin.")
    if role == "admin" and (not ADMIN_ACCESS_CODE or not secrets.compare_digest(code, ADMIN_ACCESS_CODE)):
        return error("Admin code is incorrect. Ask the project owner for authorized access.", 403)

    db = get_db()
    account = db.execute("SELECT role FROM account_roles WHERE email = ?", (email,)).fetchone()
    if account is not None and account["role"] != role:
        return error(
            "This email cannot sign in with the selected role. Contact the Host if your role needs to be changed.",
            403,
        )
    if account is None:
        # Handle older databases that may already contain session rows but not account_roles.
        existing_roles = {
            row["role"] for row in db.execute(
                "SELECT DISTINCT role FROM sessions WHERE lower(email) = ?", (email,)
            ).fetchall()
        }
        if existing_roles and (len(existing_roles) != 1 or role not in existing_roles):
            return error(
                "This email cannot sign in with the selected role. Contact the Host if your role needs to be changed.",
                403,
            )
        db.execute(
            "INSERT OR IGNORE INTO account_roles(email,role,created_at) VALUES(?,?,?)",
            (email, role, iso_now()),
        )
        account = db.execute("SELECT role FROM account_roles WHERE email = ?", (email,)).fetchone()
        if account is None or account["role"] != role:
            return error(
                "This email cannot sign in with the selected role. Contact the Host if your role needs to be changed.",
                403,
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
    code = str(data.get("hostCode", ""))
    if not HOST_EMAIL:
        return error("Host email is not configured. Set CAMPUS_HOST_EMAIL on the backend.", 503)
    if not HOST_ACCESS_CODE:
        return error("Host access is not configured. Set CAMPUS_HOST_CODE on the backend.", 503)
    if not secrets.compare_digest(email, HOST_EMAIL) or not secrets.compare_digest(code, HOST_ACCESS_CODE):
        return error("Host email or access code is incorrect.", 403)
    token = secrets.token_urlsafe(40)
    now = iso_now()
    get_db().execute(
        "INSERT INTO host_sessions(token,email,created_at,last_seen) VALUES(?,?,?,?)",
        (token, HOST_EMAIL, now, now),
    )
    return jsonify({"token": token, "host": {"name": "Host"}}), 201


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
        rows = db.execute("SELECT * FROM listings ORDER BY deadline ASC").fetchall()
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


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "API endpoint not found."}), 404
    return "Not found", 404


@app.errorhandler(500)
def server_error(_error):
    return jsonify({"error": "Unexpected server error. Check the Flask terminal for details."}), 500


init_db()

if __name__ == "__main__":
    # Local development server. Do not expose the Flask debugger to the public internet.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
