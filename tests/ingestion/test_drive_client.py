"""Tests for ingestion.drive_client.

Deliberately mocked, not live — the test suite must run without real Drive
credentials/network access. Live connectivity was verified separately (once,
manually, read-only, no writes) against the real service account and Drive
folder during Phase B — see the milestone report for that action log; it is
not re-run automatically here.

OAuth path tests (added 2026-09-02, alongside the service-account -> OAuth
switch): exercise _load_oauth_credentials and DriveClient's auth-mode
selection against a fake, locally-generated token file — never a real
interactive consent flow (not meaningfully testable without a real browser
and a real user, per the brief).
"""
from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from ingestion.drive_client import DriveCredentialError, _load_credentials


def test_missing_credentials_env_var_raises_clear_error(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    with pytest.raises(DriveCredentialError, match="not set"):
        _load_credentials(["https://www.googleapis.com/auth/drive.readonly"])


def test_nonexistent_credentials_file_raises_clear_error(monkeypatch, tmp_path):
    missing_path = tmp_path / "does-not-exist.json"
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(missing_path))
    with pytest.raises(DriveCredentialError, match="doesn't exist"):
        _load_credentials(["https://www.googleapis.com/auth/drive.readonly"])


def test_list_files_builds_expected_query_and_paginates(monkeypatch):
    from ingestion.drive_client import DriveClient

    monkeypatch.delenv("GOOGLE_OAUTH_TOKEN_PATH", raising=False)
    with mock.patch("ingestion.drive_client._load_credentials", return_value=mock.Mock()):
        with mock.patch("googleapiclient.discovery.build") as build_mock:
            service = mock.Mock()
            build_mock.return_value = service
            page1 = {
                "files": [{"id": "f1", "name": "a.csv", "mimeType": "text/csv", "modifiedTime": "2026-05-01T00:00:00Z"}],
                "nextPageToken": "tok2",
            }
            page2 = {"files": [{"id": "f2", "name": "b.csv", "mimeType": "text/csv"}]}
            service.files.return_value.list.return_value.execute.side_effect = [page1, page2]

            client = DriveClient(readwrite=False)
            files = client.list_files("root-folder-id")

    assert [f.id for f in files] == ["f1", "f2"]
    assert files[0].modified_time == "2026-05-01T00:00:00Z"
    assert files[1].modified_time is None
    assert service.files.return_value.list.call_count == 2


# ---------------------------------------------------------------------------
# Token file writing (save_oauth_token) — atomicity + restricted permissions
# ---------------------------------------------------------------------------


def test_save_oauth_token_writes_content_and_restricts_permissions(tmp_path):
    import stat

    from ingestion.drive_client import save_oauth_token

    token_path = tmp_path / "nested" / "google-oauth-token.json"
    save_oauth_token(str(token_path), '{"token": "abc"}')

    assert token_path.read_text() == '{"token": "abc"}'
    mode = stat.S_IMODE(os.stat(token_path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    # No leftover .tmp sibling once the atomic swap has completed.
    assert not (tmp_path / "nested" / "google-oauth-token.json.tmp").exists()


def test_save_oauth_token_overwrites_existing_file_atomically(tmp_path):
    from ingestion.drive_client import save_oauth_token

    token_path = tmp_path / "google-oauth-token.json"
    save_oauth_token(str(token_path), '{"token": "old"}')
    save_oauth_token(str(token_path), '{"token": "new"}')

    assert token_path.read_text() == '{"token": "new"}'
    assert not (tmp_path / "google-oauth-token.json.tmp").exists()


def test_save_oauth_token_cleans_up_tmp_file_on_write_failure(tmp_path, monkeypatch):
    from ingestion.drive_client import save_oauth_token

    token_path = tmp_path / "google-oauth-token.json"

    real_fdopen = os.fdopen

    def _boom_fdopen(fd, mode="r", *args, **kwargs):
        f = real_fdopen(fd, mode, *args, **kwargs)
        f.close()
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(os, "fdopen", _boom_fdopen)

    with pytest.raises(RuntimeError, match="simulated crash mid-write"):
        save_oauth_token(str(token_path), '{"token": "abc"}')

    # Original target file must not exist (nothing was ever written to it
    # directly) and the .tmp scratch file must have been cleaned up rather
    # than left behind half-written.
    assert not token_path.exists()
    assert not (tmp_path / "google-oauth-token.json.tmp").exists()


# ---------------------------------------------------------------------------
# OAuth credential loading (_load_oauth_credentials)
# ---------------------------------------------------------------------------


def _fake_token_json(*, expiry: str | None = None) -> str:
    """A syntactically-valid fake OAuth token file, same shape
    Credentials.to_json() produces / scripts/authorize_google_drive.py
    saves. Never a real credential — for unit tests only.
    """
    info = {
        "token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "fake-client-id.apps.googleusercontent.com",
        "client_secret": "fake-client-secret",
        "scopes": ["https://www.googleapis.com/auth/drive"],
    }
    if expiry is not None:
        info["expiry"] = expiry
    return json.dumps(info)


def test_oauth_missing_token_path_env_raises_clear_error(monkeypatch):
    from ingestion.drive_client import _load_oauth_credentials

    monkeypatch.delenv("GOOGLE_OAUTH_TOKEN_PATH", raising=False)
    with pytest.raises(DriveCredentialError, match="not set"):
        _load_oauth_credentials()


def test_oauth_nonexistent_token_file_raises_clear_error(monkeypatch, tmp_path):
    from ingestion.drive_client import _load_oauth_credentials

    missing_path = tmp_path / "does-not-exist.json"
    monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", str(missing_path))
    with pytest.raises(DriveCredentialError, match="doesn't exist"):
        _load_oauth_credentials()


def test_oauth_malformed_token_file_raises_clear_error(monkeypatch, tmp_path):
    from ingestion.drive_client import _load_oauth_credentials

    token_path = tmp_path / "google-oauth-token.json"
    token_path.write_text("this is not valid json at all {")
    monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", str(token_path))
    with pytest.raises(DriveCredentialError, match="Could not load OAuth token"):
        _load_oauth_credentials()


def test_oauth_unexpired_token_loads_without_refreshing(monkeypatch, tmp_path):
    from ingestion.drive_client import _load_oauth_credentials

    token_path = tmp_path / "google-oauth-token.json"
    far_future = "2099-01-01T00:00:00Z"
    token_path.write_text(_fake_token_json(expiry=far_future))
    monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", str(token_path))

    with mock.patch("google.oauth2.credentials.Credentials.refresh") as refresh_mock:
        credentials = _load_oauth_credentials()

    refresh_mock.assert_not_called()
    assert credentials.token == "fake-access-token"
    # File must be unchanged (still the original fake token) since no refresh happened.
    assert json.loads(token_path.read_text())["token"] == "fake-access-token"


def test_oauth_expired_token_refreshes_and_persists_new_token(monkeypatch, tmp_path):
    from ingestion.drive_client import _load_oauth_credentials

    token_path = tmp_path / "google-oauth-token.json"
    # No "expiry" key at all => from_authorized_user_info treats it as
    # already-expired (see google.oauth2.credentials source), exercising
    # the refresh path without needing to fabricate a past timestamp.
    token_path.write_text(_fake_token_json())
    monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", str(token_path))

    def fake_refresh(self, request):
        # Simulate what a real refresh does: update the in-memory token.
        self.token = "refreshed-access-token"  # noqa: SLF001 - test double

    with mock.patch("google.oauth2.credentials.Credentials.refresh", new=fake_refresh):
        credentials = _load_oauth_credentials()

    assert credentials.token == "refreshed-access-token"
    # Refreshed token must be persisted back to the same file so an
    # unattended process never needs to re-run the interactive consent flow.
    saved = json.loads(token_path.read_text())
    assert saved["token"] == "refreshed-access-token"
    assert saved["refresh_token"] == "fake-refresh-token"


def test_oauth_refresh_failure_raises_clear_error(monkeypatch, tmp_path):
    from ingestion.drive_client import _load_oauth_credentials

    token_path = tmp_path / "google-oauth-token.json"
    token_path.write_text(_fake_token_json())
    monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", str(token_path))

    def failing_refresh(self, request):
        raise RuntimeError("invalid_grant: token has been revoked")

    with mock.patch("google.oauth2.credentials.Credentials.refresh", new=failing_refresh):
        with pytest.raises(DriveCredentialError, match="could not be refreshed"):
            _load_oauth_credentials()


# ---------------------------------------------------------------------------
# DriveClient auth-mode selection
# ---------------------------------------------------------------------------


def test_driveclient_defaults_to_oauth_when_token_path_env_is_set(monkeypatch):
    from ingestion.drive_client import DriveClient

    monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", "/fake/path/token.json")
    with mock.patch("ingestion.drive_client._load_oauth_credentials", return_value=mock.Mock()) as oauth_mock:
        with mock.patch("ingestion.drive_client._load_credentials") as sa_mock:
            with mock.patch("googleapiclient.discovery.build"):
                client = DriveClient()

    assert client.auth_mode == "oauth"
    oauth_mock.assert_called_once()
    sa_mock.assert_not_called()


def test_driveclient_falls_back_to_service_account_when_no_oauth_env(monkeypatch):
    from ingestion.drive_client import DriveClient

    monkeypatch.delenv("GOOGLE_OAUTH_TOKEN_PATH", raising=False)
    with mock.patch("ingestion.drive_client._load_oauth_credentials") as oauth_mock:
        with mock.patch("ingestion.drive_client._load_credentials", return_value=mock.Mock()) as sa_mock:
            with mock.patch("googleapiclient.discovery.build"):
                client = DriveClient()

    assert client.auth_mode == "service_account"
    sa_mock.assert_called_once()
    oauth_mock.assert_not_called()


def test_driveclient_auth_mode_can_be_forced_explicitly(monkeypatch):
    from ingestion.drive_client import DriveClient

    # Even with the OAuth env var set, an explicit auth_mode overrides the default.
    monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", "/fake/path/token.json")
    with mock.patch("ingestion.drive_client._load_oauth_credentials") as oauth_mock:
        with mock.patch("ingestion.drive_client._load_credentials", return_value=mock.Mock()) as sa_mock:
            with mock.patch("googleapiclient.discovery.build"):
                client = DriveClient(auth_mode="service_account")

    assert client.auth_mode == "service_account"
    sa_mock.assert_called_once()
    oauth_mock.assert_not_called()


def test_driveclient_unknown_auth_mode_raises_value_error():
    from ingestion.drive_client import DriveClient

    with pytest.raises(ValueError, match="Unknown auth_mode"):
        DriveClient(auth_mode="carrier-pigeon")


# ---------------------------------------------------------------------------
# OAuth authorization-age tracking (write_authorized_at /
# get_token_authorization_status) — added 2026-09-25, see CLAUDE.md's
# "Google Drive OAuth token expiry" note. Every "now" reference is injected
# explicitly so these never depend on real wall-clock timing.
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402 - grouped near the tests that use it


def test_write_authorized_at_writes_iso_timestamp_sidecar(tmp_path):
    from ingestion.drive_client import write_authorized_at

    token_path = tmp_path / "google-oauth-token.json"
    when = _dt.datetime(2026, 9, 20, 12, 0, 0, tzinfo=_dt.timezone.utc)

    sidecar_path = write_authorized_at(str(token_path), when=when)

    assert sidecar_path == str(token_path) + ".authorized_at"
    written = open(sidecar_path).read().strip()
    assert written == when.isoformat()
    # No leftover .tmp sibling once the atomic swap has completed.
    assert not (tmp_path / "google-oauth-token.json.authorized_at.tmp").exists()


def test_write_authorized_at_defaults_to_now_and_overwrites(tmp_path):
    from ingestion.drive_client import write_authorized_at

    token_path = tmp_path / "google-oauth-token.json"
    write_authorized_at(str(token_path), when=_dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc))
    write_authorized_at(str(token_path))  # defaults to real now()

    sidecar_path = str(token_path) + ".authorized_at"
    written = _dt.datetime.fromisoformat(open(sidecar_path).read().strip())
    assert written.year != 2020


def test_token_authorization_status_healthy_within_4_days(tmp_path):
    from ingestion.drive_client import get_token_authorization_status, write_authorized_at

    token_path = str(tmp_path / "token.json")
    now = _dt.datetime(2026, 9, 25, 12, 0, 0, tzinfo=_dt.timezone.utc)
    write_authorized_at(token_path, when=now - _dt.timedelta(days=2))

    result = get_token_authorization_status(token_path, now=now)

    assert result.status == "healthy"
    assert result.days_elapsed == pytest.approx(2.0, abs=0.01)
    assert result.authorized_at is not None


def test_token_authorization_status_expiring_soon_between_5_and_7_days(tmp_path):
    from ingestion.drive_client import get_token_authorization_status, write_authorized_at

    token_path = str(tmp_path / "token.json")
    now = _dt.datetime(2026, 9, 25, 12, 0, 0, tzinfo=_dt.timezone.utc)
    write_authorized_at(token_path, when=now - _dt.timedelta(days=6))

    result = get_token_authorization_status(token_path, now=now)

    assert result.status == "expiring_soon"
    assert result.days_elapsed == pytest.approx(6.0, abs=0.01)


def test_token_authorization_status_likely_expired_past_7_days(tmp_path):
    from ingestion.drive_client import get_token_authorization_status, write_authorized_at

    token_path = str(tmp_path / "token.json")
    now = _dt.datetime(2026, 9, 25, 12, 0, 0, tzinfo=_dt.timezone.utc)
    write_authorized_at(token_path, when=now - _dt.timedelta(days=9))

    result = get_token_authorization_status(token_path, now=now)

    assert result.status == "likely_expired"
    assert result.days_elapsed == pytest.approx(9.0, abs=0.01)


def test_token_authorization_status_unknown_when_sidecar_missing(tmp_path):
    from ingestion.drive_client import get_token_authorization_status

    token_path = str(tmp_path / "token-with-no-sidecar.json")

    result = get_token_authorization_status(token_path)

    assert result.status == "unknown"
    assert result.authorized_at is None
    assert result.days_elapsed is None


def test_token_authorization_status_unknown_when_token_path_not_configured(monkeypatch):
    from ingestion.drive_client import get_token_authorization_status

    monkeypatch.delenv("GOOGLE_OAUTH_TOKEN_PATH", raising=False)

    result = get_token_authorization_status()

    assert result.status == "unknown"


def test_token_authorization_status_unknown_on_malformed_sidecar(tmp_path):
    from ingestion.drive_client import get_token_authorization_status

    token_path = tmp_path / "token.json"
    sidecar_path = str(token_path) + ".authorized_at"
    with open(sidecar_path, "w") as f:
        f.write("not a valid timestamp")

    result = get_token_authorization_status(str(token_path))

    assert result.status == "unknown"


def test_token_authorization_status_never_raises_on_unreadable_directory(tmp_path):
    """Sanity check for the graceful-degradation contract: a token_path
    that isn't even a real file path (e.g. pointing inside a directory that
    doesn't exist) still returns "unknown", never raises."""
    from ingestion.drive_client import get_token_authorization_status

    token_path = str(tmp_path / "does" / "not" / "exist" / "token.json")

    result = get_token_authorization_status(token_path)

    assert result.status == "unknown"
