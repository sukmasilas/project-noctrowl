"""One-time local Google Drive OAuth authorization script.

WHY THIS EXISTS: the Google service account this project used previously
can only READ the real "Finance & Accounting" Drive folder — it has zero
storage quota on a personal Gmail Drive outside a Shared Drive (which
requires paid Google Workspace), so any attempt to create a folder or
upload a file fails with 403 storageQuotaExceeded (confirmed against the
live Drive API). Authorizing as the real Google user instead (OAuth, this
script) grants access under that user's own storage quota, with both read
and write.

RUN THIS YOURSELF, ONCE, LOCALLY:
  - On your own machine, logged in to your own Google account (the one
    that owns the "Finance & Accounting" Drive folder) — not on the
    droplet, not by an agent. It needs a real browser and a real human to
    click "Allow".
  - Safe to re-run any time you need to regenerate the token (lost,
    revoked, or you just want a fresh grant) — it always runs the
    interactive consent flow again and overwrites the existing token file.

WHAT IT DOES:
  1. Reads the OAuth "Desktop app" client-secret JSON you downloaded from
     Google Cloud Console (path from the GOOGLE_OAUTH_CLIENT_SECRET env
     var — see .env.example).
  2. Opens your default browser to Google's sign-in/consent screen
     (InstalledAppFlow.run_local_server) with the full Drive scope
     (https://www.googleapis.com/auth/drive) — log in as yourself and
     click Allow. Forces Google's consent screen every run (prompt=
     "consent") so a refresh token is always issued, even on a re-run —
     without this, only the very first-ever consent for this OAuth client
     is guaranteed to include one.
  3. Saves the resulting credentials (including a refresh token) as JSON
     to the GOOGLE_OAUTH_TOKEN_PATH env var's path (see .env.example).
     ingestion/drive_client.py reads from this same path afterward and
     refreshes the access token automatically when it expires — you should
     not need to re-run this script under normal use.
  4. Writes a small sidecar file (<token path>.authorized_at) recording the
     UTC timestamp of this authorization. The Documents screen reads this to
     show a proactive warning as the ~7-day Testing-mode refresh-token cap
     approaches (see CLAUDE.md's "Google Drive OAuth token expiry" note).

USAGE:
    python3 scripts/authorize_google_drive.py
"""
from __future__ import annotations

import os
import sys

# Allow this script to be run directly (e.g. `python3 scripts/authorize_google_drive.py`
# from the project root, as documented in USAGE above). When invoked that way, Python
# sets sys.path[0] to this file's own directory (scripts/), not the project root, so the
# top-level `ingestion` package (which lives at the project root) wouldn't otherwise be
# importable. Insert the project root explicitly before that import is reached.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCOPES = ["https://www.googleapis.com/auth/drive"]

DEFAULT_CLIENT_SECRET_PATH = "./secrets/google-oauth-client-secret.json"
DEFAULT_TOKEN_PATH = "./secrets/google-oauth-token.json"


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass  # python-dotenv is a project dependency, but don't hard-fail if it's missing here

    client_secret_path = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", DEFAULT_CLIENT_SECRET_PATH)
    token_path = os.environ.get("GOOGLE_OAUTH_TOKEN_PATH", DEFAULT_TOKEN_PATH)

    if not os.path.isfile(client_secret_path):
        print(
            f"ERROR: OAuth client-secret file not found at {client_secret_path!r}.\n\n"
            "Get it from Google Cloud Console:\n"
            "  APIs & Services > Credentials > Create Credentials > OAuth client ID\n"
            "  > Application type: Desktop app > Create > Download JSON\n\n"
            f"Save the downloaded file at {client_secret_path!r}, or set "
            "GOOGLE_OAUTH_CLIENT_SECRET in your .env to point at wherever you saved it.",
            file=sys.stderr,
        )
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from oauthlib.oauth2 import OAuth2Error
    except ImportError:
        print(
            "ERROR: google-auth-oauthlib is not installed. Run:\n"
            "  pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    # ingestion.drive_client is this same project's Drive layer — reused
    # here only for its atomic/restricted-permissions token-file writer
    # (save_oauth_token), so the initial save and the later refresh-save
    # (see ingestion/drive_client.py::_load_oauth_credentials) go through
    # the exact same, already-tested write path rather than two versions
    # that could drift.
    from ingestion.drive_client import save_oauth_token, write_authorized_at

    print("=" * 70)
    print("Google Drive authorization")
    print("=" * 70)
    print()
    print(f"Client secret: {client_secret_path}")
    print(f"Token will be saved to: {token_path}")
    print()
    print("Your browser will now open. Sign in as the Google account that owns")
    print("the 'Finance & Accounting' Drive folder, and click Allow when asked")
    print("to grant this app access to your Google Drive.")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
    try:
        # prompt="consent" forces Google to issue a refresh token on every
        # run, not just the very first time this client ever got consent —
        # without it, a re-run to regenerate a lost token could silently
        # come back with no refresh token at all, which would contradict
        # this script's own "safe to re-run any time" claim above.
        credentials = flow.run_local_server(port=0, prompt="consent")
    except OAuth2Error as exc:
        print(
            f"ERROR: Google did not complete the authorization: {exc}\n\n"
            "This usually means access was denied or the consent screen was closed\n"
            "before finishing. Re-run this script and click Allow when prompted.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - port conflicts, browser launch failure, timeout, etc.
        print(
            f"ERROR: The authorization flow did not complete: {exc}\n\n"
            "Re-run this script and complete the browser sign-in/consent steps promptly.",
            file=sys.stderr,
        )
        return 1

    # Atomic + created-already-restricted (0600) — see
    # ingestion/drive_client.py::save_oauth_token for why a plain
    # open(path, "w") + chmod afterward isn't good enough for a file
    # holding a live refresh token.
    save_oauth_token(token_path, credentials.to_json())

    # Record when this real, interactive authorization completed, so the
    # Documents screen can proactively warn as it approaches Google's known
    # ~7-day refresh-token cap for apps in "Testing" publishing status (see
    # CLAUDE.md's "Google Drive OAuth token expiry" note and
    # ingestion/drive_client.py's get_token_authorization_status). This is a
    # separate sidecar file, not a field inside the token JSON itself — see
    # write_authorized_at's docstring for why.
    authorized_at_path = write_authorized_at(token_path)

    print()
    print("SUCCESS.")
    print()
    print("Granted scope:")
    for scope in SCOPES:
        print(f"  - {scope}")
    print()
    print(f"Token (including a refresh token) saved to: {token_path}")
    print(f"Authorization timestamp recorded to: {authorized_at_path}")
    print("(used by the Documents screen to warn before Google's ~7-day Testing-mode cap hits)")
    print()
    print("This file lets the app read AND write your Google Drive under your own")
    print("account's storage quota — it's already gitignored, but treat it like a")
    print("password (don't paste it into chat, don't commit it, don't share it).")
    print()
    print("Next step: make sure GOOGLE_OAUTH_TOKEN_PATH is set in your .env to the")
    print("path above (it already matches the default if you didn't override it),")
    print("then the app will use this token automatically for Drive access — no")
    print("further action needed unless the token is later lost or revoked, in")
    print("which case just re-run this script.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
