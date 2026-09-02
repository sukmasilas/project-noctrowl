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
_has_oauth = bool(os.environ.get("GOOGLE_OAUTH_TOKEN_PATH"))
_has_service_account = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
if os.environ.get("GOOGLE_DRIVE_ROOT_FOLDER_ID") and (_has_oauth or _has_service_account):
    try:
        from ingestion.drive_client import DriveClient

        # DriveClient itself defaults to OAuth when GOOGLE_OAUTH_TOKEN_PATH is set
        # (see ingestion/drive_client.py), falling back to the legacy service-account
        # path otherwise — no auth_mode needs to be chosen here.
        drive_client = DriveClient()
    except Exception:  # noqa: BLE001 - Drive isn't required for the app to boot; Sync Now will just report it's unavailable
        drive_client = None

app = create_app(drive_client=drive_client)

if __name__ == "__main__":
    app.run(debug=True)
