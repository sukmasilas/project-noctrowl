"""Local/production entrypoint for the Flask app.

Usage (dev): FLASK_APP=wsgi.py flask run
Usage (droplet, milestone 5+): a real WSGI server (gunicorn wsgi:app) —
not decided/deployed yet, this file just needs to exist and expose ``app``
either way.

Loads .env via python-dotenv (already a milestone-2 dependency, unused
until now) so DATABASE_URL/APP_SECRET_KEY/etc. are available the same way
whether you run this directly or via `flask run`.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

from webapp import create_app  # noqa: E402 - must follow load_dotenv()

drive_client = None
if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and os.environ.get("GOOGLE_DRIVE_ROOT_FOLDER_ID"):
    try:
        from ingestion.drive_client import DriveClient

        drive_client = DriveClient()
    except Exception:  # noqa: BLE001 - Drive isn't required for the app to boot; Sync Now will just report it's unavailable
        drive_client = None

app = create_app(drive_client=drive_client)

if __name__ == "__main__":
    app.run(debug=True)
