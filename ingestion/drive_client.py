"""Thin Google Drive read layer.

Implements docs/design/milestone-3-ingestion-design.md §8. Deliberately
narrow: list files in a folder, download a file's bytes, and a
find-or-create-folder helper (used only as a one-time idempotent "make sure
this month's folder exists" utility for testing against a real Drive
folder — NOT wired to any schedule; automatic month-ahead provisioning is
milestone 5, per CLAUDE.md's Scheduling section and the brief's explicit
scope boundary).

Auth: a service-account credentials file, path from the
``GOOGLE_APPLICATION_CREDENTIALS`` env var — never hardcoded, matching
``ledger/db.py``'s existing pattern for ``DATABASE_URL``.

Scope note (design doc open question 10, resolved by Main-agent 2026-08-31:
"proceed exactly as planned — test empirically when you reach the live
Drive layer; if the credential's scope turns out inadequate, stop and
report rather than guessing a workaround"): this module requests
``drive.readonly`` (list/download) — the "Finance & Accounting" folder tree
is user-created, not created by the service account, so the narrower
``drive.file`` scope (which only sees files/folders the service account
itself created) would see nothing. ``find_or_create_folder`` additionally
needs write access, so it separately requests the broader ``drive`` scope —
kept as a SEPARATE, explicitly-named credential-scope constant so a future
caller can request read-only access without ever implicitly getting write
access it didn't ask for.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

READONLY_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
READWRITE_SCOPES = ["https://www.googleapis.com/auth/drive"]

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
_FOLDER_MIME_TYPE = FOLDER_MIME_TYPE  # kept for internal call sites below


class DriveCredentialError(Exception):
    """Raised when GOOGLE_APPLICATION_CREDENTIALS is missing, unreadable, or
    the resulting credentials can't authenticate — never silently proceeds
    without real credentials, and never fabricates a folder/file ID.
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


class DriveClient:
    """Wraps the Drive API v3. Construct with ``readwrite=True`` only when
    you actually need ``find_or_create_folder`` — everything else only ever
    needs read-only scope.
    """

    def __init__(self, *, readwrite: bool = False):
        from googleapiclient.discovery import build

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

    def find_or_create_folder(self, parent_id: str, name: str) -> str:
        """Idempotent get-or-create by name under a parent folder. Requires
        readwrite=True at construction time. NOT wired to any schedule —
        see module docstring.
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
