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
ADMIN_ACCESS_CODE = os.environ.get("CAMPUS_ADMIN_CODE", "CAMPUS-ADMIN-2026")

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
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','student')),
    created_at TEXT NOT NULL
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
    # Keep reserving students' names and email addresses private in the public listing API.
    return {
        "id": row["id"], "name": row["name"], "quantity": row["quantity"],
        "location": row["location"],
        "contactNumber": row["contact_number"],
        "pricePerPortion": row["price_per_portion"],
        "deadline": row["deadline"],
        "emoji": row["emoji"], "status": row["status"],
        "createdAt": row["created_at"], "createdBy": row["created_by"],
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
    if required_role and session["role"] != required_role:
        return None, error(f"Only {required_role}s can perform this action.", 403)
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
    if role not in ("student", "admin"):
        return error("Choose student or admin.")
    if role == "admin" and (not ADMIN_ACCESS_CODE or not secrets.compare_digest(code, ADMIN_ACCESS_CODE)):
        return error("Admin code is incorrect. Ask the project owner for authorized access.", 403)

    token = secrets.token_urlsafe(32)
    db = get_db()
    db.execute(
        "INSERT INTO sessions(token,name,email,role,created_at) VALUES(?,?,?,?,?)",
        (token, name, email, role, iso_now()),
    )
    return jsonify({"token": token, "user": {"name": name, "email": email, "role": role}}), 201


@app.get("/api/session")
def get_session():
    session, failure = current_session()
    if failure:
        return failure
    return jsonify({"user": {"name": session["name"], "email": session["email"], "role": session["role"]}})


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
        "INSERT INTO listings(id,name,quantity,location,contact_number,price_per_portion,deadline,emoji,status,created_by,created_at) "
        "VALUES(?,?,?,?,?,?,?,?, 'available',?,?)",
        (listing_id, name, quantity, location, contact_number, price_per_portion,
         deadline.isoformat(timespec="seconds"), emoji, session["email"], iso_now()),
    )
    row = db.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    return jsonify({"listing": listing_to_dict(row, db)}), 201


@app.post("/api/listings/<listing_id>/reserve")
def reserve_portion(listing_id: str):
    session, failure = current_session("student")
    if failure:
        return failure
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
        db.execute(
            "INSERT INTO reservations(listing_id,user_email,user_name,reserved_at,status) VALUES(?,?,?,?, 'reserved')",
            (listing_id, session["email"], session["name"], iso_now()),
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
