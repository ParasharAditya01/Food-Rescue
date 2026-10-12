# Campus Food Rescue

Flask + SQLite prototype for sharing surplus campus food.

## Run locally

```bash
python -m pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`. Keep `app.py`, `index.html`, and `requirements.txt` in the same directory. SQLite defaults to `database.db`; set `CAMPUS_FOOD_DB` to use another path.

## Configure Admin and Host sign-in

Admin and Host use email + password credentials configured as environment variables. No access codes are used. Do not put passwords in source code or commit them to GitHub.

Required environment variables:

- `CAMPUS_ADMIN_EMAIL` — exact email allowed to sign in as Admin
- `CAMPUS_ADMIN_PASSWORD` — Admin password
- `CAMPUS_HOST_EMAIL` — exact email allowed to sign in to the Host Control Panel
- `CAMPUS_HOST_PASSWORD` — Host password

The shorter aliases `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `HOST_EMAIL`, and `HOST_PASSWORD` are also accepted, but the `CAMPUS_` names are preferred. Set all four values in Render under **Environment** and redeploy. Use separate, strong passwords for Admin and Host.

Students can join using their name and email. The prototype does not verify student email ownership. Admin and Host credentials are checked on the server and are never stored in the SQLite database.

## Listing lifecycle

- Listings can be reserved only before their collection deadline and while portions remain.
- Once the deadline passes, the backend sets the listing to `expired`, sets its available quantity to zero, and blocks reservations.
- Expired listings remain visible for 24 hours after the later of their creation time or collection deadline. They display **Expired — no more booking** and are then hidden from the public listing page.
- Host dashboard history remains available in the database.

## Deployment notes

The Render web service should start with `gunicorn app:app`. Ensure `requirements.txt` includes Flask and Gunicorn. For persistent SQLite data on Render, configure a persistent disk and set `CAMPUS_FOOD_DB` to a path on that disk; otherwise data may be lost when the service instance is replaced.
