"""Tests for ingestion.drive_client.

Deliberately mocked, not live — the test suite must run without real Drive
credentials/network access. Live connectivity was verified separately (once,
manually, read-only, no writes) against the real service account and Drive
folder during Phase B — see the milestone report for that action log; it is
not re-run automatically here.
"""
from __future__ import annotations

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
