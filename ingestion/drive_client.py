"""Thin Google Drive access layer.

Implements docs/design/milestone-3-ingestion-design.md §8. Deliberately
narrow: list files in a folder, download a file's bytes, upload a file, and
a find-or-create-folder helper (used only as a one-time idempotent "make
sure this month's folder exists" utility for testing against a real Drive
folder — NOT wired to any schedule; automatic month-ahead provisioning is
milestone 5, per CLAUDE.md's Scheduling section and the brief's explicit
scope boundary).

Auth: TWO credential paths exist. OAuth (user consent) is the default/
primary path once configured; the service account is a legacy/read-only
fallback kept working, not deleted.

Found 2026-09-01, confirmed against the live Drive API (403
storageQuotaExceeded): a service account gets ZERO storage quota on a
personal Gmail Drive outside a Shared Drive (which requires paid Google
Workspace) — it can list/download files the user already put in the real
"Finance & Accounting" folder, but it can never create a folder or upload a
file there. That's a hard blocker for milestone 5's automatic folder
provisioning and any write path in general, so Drive access moved to real
user OAuth (the user's own Google account, via
``scripts/authorize_google_drive.py`` — a one-time interactive local
consent flow) as the active credential source for both read and write.

Service-account path (original, still present): a service-account
credentials file, path from the ``GOOGLE_APPLICATION_CREDENTIALS`` env var
— never hardcoded, matching ``ledger/db.py``'s existing pattern for
``DATABASE_URL``. Kept for the read-only path and in case the project ever
moves to Google Workspace + Shared Drives, where service accounts do get
their own quota. Scope note (design doc open question 10, resolved by
Main-agent 2026-08-31): this module requests ``drive.readonly`` (list/
download) — the "Finance & Accounting" folder tree is user-created, not
created by the service account, so the narrower ``drive.file`` scope
(which only sees files/folders the service account itself created) would
see nothing. ``find_or_create_folder``/``upload_file`` additionally need
write access, so they separately request the broader ``drive`` scope when
running under the service-account path — kept as a SEPARATE,
explicitly-named credential-scope constant so a future caller can request
read-only access without ever implicitly getting write access it didn't
ask for.

OAuth path (added 2026-09-02): a single ``drive`` scope (read + write)
loaded from a token file generated once, locally, by
``scripts/authorize_google_drive.py``, at the path in the
``GOOGLE_OAUTH_TOKEN_PATH`` env var. Access tokens expire; the refresh
token normally doesn't (under normal use) — ``_load_oauth_credentials``
refreshes automatically when needed and re-saves the refreshed token back
to the same file, so an unattended backend process never needs to re-run
the interactive consent flow.
"""
from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass

READONLY_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
READWRITE_SCOPES = ["https://www.googleapis.com/auth/drive"]
OAUTH_SCOPES = ["https://www.googleapis.com/auth/drive"]

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
_FOLDER_MIME_TYPE = FOLDER_MIME_TYPE  # kept for internal call sites below


class DriveCredentialError(Exception):
    """Raised when Drive credentials (service-account or OAuth) are
    missing, unreadable, or the resulting credentials can't authenticate —
    never silently proceeds without real credentials, and never fabricates
    a folder/file ID.
    """


@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str
    modified_time: str | None


def _load_credentials(scopes: list[str]):
    from google.oauth2 import service_account

    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not cred_path:
        raise DriveCredentialError(
            "GOOGLE_APPLICATION_CREDENTIALS is not set. Copy .env.example to .env and point it "
            "at the service-account JSON key file (never hardcode credentials in code)."
        )
    if not os.path.isfile(cred_path):
        raise DriveCredentialError(
            f"GOOGLE_APPLICATION_CREDENTIALS points at {cred_path!r}, which doesn't exist."
        )
    try:
        return service_account.Credentials.from_service_account_file(cred_path, scopes=scopes)
    except Exception as exc:  # malformed JSON, wrong key shape, etc.
        raise DriveCredentialError(f"Could not load service-account credentials from {cred_path!r}: {exc}") from exc


def save_oauth_token(token_path: str, content: str) -> None:
    """Write an OAuth token file atomically and with restricted (0600)
    permissions from the moment it's created.

    Two things a naive ``open(path, "w")`` gets wrong for a file holding a
    live refresh token:
      - Permission window: create-then-chmod leaves the file briefly at the
        OS default (typically 0644) before it's locked down.
      - Atomicity: writing directly into the target file means a crash/
        OOM-kill mid-write (the droplet runs at 1GB RAM — see CLAUDE.md)
        truncates a working token file for no good reason. Recovery is
        bounded (a clear DriveCredentialError telling the user to re-run
        the authorization script) but needlessly destructive.

    Fixed by creating the file already-restricted via ``os.open`` with
    explicit flags/mode, writing to a ``.tmp`` sibling, then swapping it
    into place with ``os.replace`` (atomic on POSIX and Windows, and
    preserves the .tmp file's own 0600 mode across the rename).
    Used both for the initial save (scripts/authorize_google_drive.py) and
    the refresh-save below.
    """
    token_dir = os.path.dirname(token_path) or "."
    os.makedirs(token_dir, exist_ok=True)
    tmp_path = token_path + ".tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    os.replace(tmp_path, token_path)


def _load_oauth_credentials():
    """Load OAuth user credentials from GOOGLE_OAUTH_TOKEN_PATH, refreshing
    the access token (and re-persisting the refreshed token to the same
    file) when it's expired. Raises DriveCredentialError, never silently
    proceeds without real credentials — same contract as _load_credentials.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials as UserCredentials

    token_path = os.environ.get("GOOGLE_OAUTH_TOKEN_PATH")
    if not token_path:
        raise DriveCredentialError(
            "GOOGLE_OAUTH_TOKEN_PATH is not set. Run scripts/authorize_google_drive.py once "
            "to generate it, then set GOOGLE_OAUTH_TOKEN_PATH in .env (see .env.example)."
        )
    if not os.path.isfile(token_path):
        raise DriveCredentialError(
            f"GOOGLE_OAUTH_TOKEN_PATH points at {token_path!r}, which doesn't exist. "
            "Run scripts/authorize_google_drive.py once to generate it."
        )
    try:
        credentials = UserCredentials.from_authorized_user_file(token_path, scopes=OAUTH_SCOPES)
    except Exception as exc:  # malformed JSON, wrong shape, etc.
        raise DriveCredentialError(f"Could not load OAuth token from {token_path!r}: {exc}") from exc

    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception as exc:
            raise DriveCredentialError(
                f"Google OAuth token at {token_path!r} could not be refreshed (it may have been "
                f"revoked): {exc}. Re-run scripts/authorize_google_drive.py to re-authorize."
            ) from exc
        # Persist the refreshed access token so an unattended process (this
        # one) never has to re-run the interactive consent flow just
        # because the short-lived access token expired.
        try:
            save_oauth_token(token_path, credentials.to_json())
        except OSError:
            pass  # refreshed token still works for this run even if we couldn't persist it

    return credentials


# ---------------------------------------------------------------------------
# OAuth authorization-age tracking (added 2026-09-25)
#
# WHY THIS EXISTS: see CLAUDE.md's "Google Drive OAuth token expiry — known
# limitation" note. The OAuth consent screen backing this project is in
# Google's "Testing" publishing status, which caps refresh tokens at ~7
# days. Google exposes no real, retrievable server-side expiry timestamp
# for a refresh token — the cap is only discoverable when a refresh attempt
# actually fails (surfaced as DriveCredentialError above). This section adds
# a best-effort, LOCAL estimate — "how long ago did a human last complete
# the real interactive consent flow" — so the Documents screen can show a
# proactive heads-up before Sync Now breaks again, instead of only after.
#
# This is deliberately NOT baked into the token JSON file itself
# (GOOGLE_OAUTH_TOKEN_PATH): that file is parsed by google-auth's
# ``UserCredentials.from_authorized_user_file``, a well-tested third-party
# format we don't want to risk destabilizing with a custom field. Instead,
# a small sidecar text file lives next to it, written ONLY by
# scripts/authorize_google_drive.py at the moment a real human consent flow
# completes — never by the automatic access-token refresh in
# _load_oauth_credentials above, since a routine refresh isn't a new
# consent event and re-stamping it would defeat the whole point of tracking
# staleness.
# ---------------------------------------------------------------------------

# Boundaries (days elapsed since the last real authorization) for the
# status buckets returned by get_token_authorization_status. Chosen to
# leave a few days' warning window before Google's known ~7-day cap:
# 0-4 days = healthy, 5-7 = expiring soon (the warning zone), >7 = likely
# expired.
TOKEN_AUTHORIZATION_WARNING_DAYS = 5
TOKEN_AUTHORIZATION_EXPIRED_DAYS = 7


def _authorized_at_sidecar_path(token_path: str) -> str:
    return token_path + ".authorized_at"


def write_authorized_at(token_path: str, when: _dt.datetime | None = None) -> str:
    """Record that a real interactive OAuth authorization just completed for
    ``token_path``, by writing an ISO-8601 UTC timestamp to a sidecar file
    (``<token_path>.authorized_at``) alongside it.

    Called ONLY by scripts/authorize_google_drive.py, as its last step right
    after the token itself is saved via save_oauth_token. Nothing else in
    this module (in particular, _load_oauth_credentials' automatic
    access-token refresh) should ever call this — a refresh is not a new
    human consent event.

    Returns the sidecar path written, so the caller can print/confirm it.
    """
    if when is None:
        when = _dt.datetime.now(_dt.timezone.utc)
    sidecar_path = _authorized_at_sidecar_path(token_path)
    sidecar_dir = os.path.dirname(sidecar_path) or "."
    os.makedirs(sidecar_dir, exist_ok=True)
    # Not a secret (just a timestamp), but written atomically anyway (tmp +
    # os.replace) for the same "don't leave a half-written file behind on a
    # crash mid-write" reason save_oauth_token above cares about.
    tmp_path = sidecar_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(when.isoformat())
    os.replace(tmp_path, sidecar_path)
    return sidecar_path


@dataclass
class TokenAuthorizationStatus:
    status: str  # "healthy" | "expiring_soon" | "likely_expired" | "unknown"
    authorized_at: _dt.datetime | None
    days_elapsed: float | None


def get_token_authorization_status(
    token_path: str | None = None, *, now: _dt.datetime | None = None
) -> TokenAuthorizationStatus:
    """Best-effort estimate of how stale the current Drive OAuth
    authorization is, for the Documents screen's proactive warning banner.

    This is an ESTIMATE, not a confirmed fact — see the module-level note
    above for why Google gives us nothing more authoritative to check
    against. Degrades gracefully to "unknown" and never raises: a missing
    ``token_path``/env var, a missing sidecar file (e.g. a token generated
    before this feature existed, or Drive not configured at all), or an
    unreadable/malformed timestamp are all treated the same way as every
    other "no data yet" case in this app — nothing to warn about, nothing
    to crash over.

    ``token_path`` defaults to the ``GOOGLE_OAUTH_TOKEN_PATH`` env var (the
    same one ``_load_oauth_credentials`` reads). ``now`` is injectable so
    tests never depend on real wall-clock timing.
    """
    if token_path is None:
        token_path = os.environ.get("GOOGLE_OAUTH_TOKEN_PATH")
    if not token_path:
        return TokenAuthorizationStatus(status="unknown", authorized_at=None, days_elapsed=None)

    sidecar_path = _authorized_at_sidecar_path(token_path)
    try:
        with open(sidecar_path, "r") as f:
            raw = f.read().strip()
        authorized_at = _dt.datetime.fromisoformat(raw)
    except (OSError, ValueError):
        return TokenAuthorizationStatus(status="unknown", authorized_at=None, days_elapsed=None)

    if authorized_at.tzinfo is None:
        authorized_at = authorized_at.replace(tzinfo=_dt.timezone.utc)

    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    days_elapsed = (now - authorized_at).total_seconds() / 86400.0

    if days_elapsed < TOKEN_AUTHORIZATION_WARNING_DAYS:
        status = "healthy"
    elif days_elapsed <= TOKEN_AUTHORIZATION_EXPIRED_DAYS:
        status = "expiring_soon"
    else:
        status = "likely_expired"

    return TokenAuthorizationStatus(status=status, authorized_at=authorized_at, days_elapsed=days_elapsed)


class DriveClient:
    """Wraps the Drive API v3.

    Auth mode defaults to OAuth when ``GOOGLE_OAUTH_TOKEN_PATH`` is set
    (the primary path once the user has run
    ``scripts/authorize_google_drive.py``), otherwise falls back to the
    legacy service-account path. Pass ``auth_mode="oauth"`` or
    ``auth_mode="service_account"`` to force one explicitly.

    ``readwrite`` only matters for the service-account path (it selects
    which scope to request — see module docstring). Under OAuth, the single
    ``drive`` scope already covers both read and write, so ``readwrite`` is
    accepted but has no effect — kept so existing callers that pass it
    don't break.
    """

    def __init__(self, *, readwrite: bool = False, auth_mode: str | None = None):
        from googleapiclient.discovery import build

        mode = auth_mode or ("oauth" if os.environ.get("GOOGLE_OAUTH_TOKEN_PATH") else "service_account")
        if mode not in ("oauth", "service_account"):
            raise ValueError(f"Unknown auth_mode {mode!r}; expected 'oauth' or 'service_account'.")
        self.auth_mode = mode

        if mode == "oauth":
            credentials = _load_oauth_credentials()
        else:
            scopes = READWRITE_SCOPES if readwrite else READONLY_SCOPES
            credentials = _load_credentials(scopes)
        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)

    def list_files(self, folder_id: str, mime_types: list[str] | None = None) -> list[DriveFile]:
        query = f"'{folder_id}' in parents and trashed = false"
        if mime_types:
            mime_query = " or ".join(f"mimeType = '{mt}'" for mt in mime_types)
            query += f" and ({mime_query})"

        files: list[DriveFile] = []
        page_token = None
        while True:
            response = (
                self._service.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
                    pageToken=page_token,
                )
                .execute()
            )
            for f in response.get("files", []):
                files.append(DriveFile(id=f["id"], name=f["name"], mime_type=f["mimeType"], modified_time=f.get("modifiedTime")))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return files

    def download_file(self, file_id: str) -> bytes:
        import io

        from googleapiclient.http import MediaIoBaseDownload

        request = self._service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
        return buffer.getvalue()

    def upload_file(self, parent_id: str, name: str, content: bytes, mime_type: str) -> str:
        """Idempotent get-or-create-by-name file upload under a parent
        folder. Requires write access: under OAuth (the default once
        configured) this is automatic (the single ``drive`` scope covers
        it); under the legacy service-account path, construct with
        ``readwrite=True``. Added 2026-09 for the one-time real-document
        upload pass (see docs/build-briefs — live-Drive validation run) —
        not previously needed since ingestion.sync only ever reads from
        Drive. If a non-folder file with this exact name already exists
        directly under the given parent, its id is returned unchanged
        rather than creating a duplicate — safe to re-run this against the
        same Drive folder without piling up duplicate uploads.
        """
        import io

        from googleapiclient.http import MediaIoBaseUpload

        escaped_name = name.replace("'", "\\'")
        query = (
            f"'{parent_id}' in parents and trashed = false and mimeType != '{_FOLDER_MIME_TYPE}' "
            f"and name = '{escaped_name}'"
        )
        response = self._service.files().list(q=query, fields="files(id, name)").execute()
        existing = response.get("files", [])
        if existing:
            return existing[0]["id"]

        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
        created = (
            self._service.files()
            .create(body={"name": name, "parents": [parent_id]}, media_body=media, fields="id")
            .execute()
        )
        return created["id"]

    def find_or_create_folder(self, parent_id: str, name: str) -> str:
        """Idempotent get-or-create by name under a parent folder. Requires
        write access — see upload_file's docstring for how that's satisfied
        under OAuth vs. the service-account path. NOT wired to any
        schedule — see module docstring.
        """
        escaped_name = name.replace("'", "\\'")
        query = (
            f"'{parent_id}' in parents and trashed = false and mimeType = '{_FOLDER_MIME_TYPE}' "
            f"and name = '{escaped_name}'"
        )
        response = self._service.files().list(q=query, fields="files(id, name)").execute()
        existing = response.get("files", [])
        if existing:
            return existing[0]["id"]

        created = (
            self._service.files()
            .create(body={"name": name, "mimeType": _FOLDER_MIME_TYPE, "parents": [parent_id]}, fields="id")
            .execute()
        )
        return created["id"]
