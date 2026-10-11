# Campus Food Rescue — Flask + SQLite

The existing frontend is served by Flask and communicates with the backend API. Listings, remaining portions, reservations, and sessions are stored in `database.db` (created automatically beside `app.py`).

## Requirements

- Python 3.10+
- pip

## Run on macOS / Linux

From this folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export CAMPUS_ADMIN_CODE='choose-a-private-admin-code'
export SMTP_HOST='smtp.gmail.com'
export SMTP_PORT='587'
export SMTP_USERNAME='your-sender@gmail.com'
export SMTP_PASSWORD='your-16-character-app-password'
export SMTP_FROM_EMAIL='your-sender@gmail.com'
python app.py
```

Then open **http://127.0.0.1:5000**. Do not open `index.html` directly as a `file://` URL; it must be served by Flask for `/api/...` requests to work.

On Windows, activate the virtual environment with `.venv\\Scripts\\activate`, then set the environment variable in PowerShell with `$env:CAMPUS_ADMIN_CODE='choose-a-private-admin-code'` before running `python app.py`.

## Implemented behavior

- Admins create listings through the authenticated API.
- Listing data and reservations persist in SQLite across refreshes and app restarts.
- Every reservation reduces quantity by exactly one.
- Reservation check/decrement runs in a SQLite `BEGIN IMMEDIATE` transaction, preventing concurrent requests from reserving more portions than remain.
- When a deadline passes, the backend sets any still-available quantity to zero and marks the listing expired. Expired listings cannot be reserved.
- API errors return useful messages and HTTP status codes.

## API endpoints

- `GET /api/health` — server/database health check
- `POST /api/email-otp` — send a six-digit email verification code (JSON: `name`, `email`, `role`, and `adminCode` for admins)
- `POST /api/email-otp/verify` — verify a code and create a session (JSON: `challengeId`, `otp`)
- `POST /api/session` — disabled; direct unverified session creation is rejected
- `DELETE /api/session` — end the current session
- `GET /api/listings` — list listings and reservation history
- `POST /api/listings` — admin-only listing creation
- `POST /api/listings/<id>/reserve` — student-only one-portion reservation

## Security / deployment note

This is a local hackathon prototype, not production authentication. Email ownership is verified by a six-digit OTP sent through configured SMTP before the backend creates a session. Codes expire after 10 minutes, are single-use, allow five attempts, and a new code is rate-limited to once per 60 seconds per email. Email delivery will fail closed until SMTP variables are configured. The admin code is checked server-side before an admin OTP is sent; set `CAMPUS_ADMIN_CODE` to a private value before using it. For a public deployment, add verified user authentication, CSRF/rate-limiting protections as appropriate, and a production WSGI server.
