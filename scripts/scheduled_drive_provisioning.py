"""Cron entrypoint: automatic monthly Google Drive folder provisioning.

Implements CLAUDE.md's "Scheduling & triggers" section: "Monthly Drive
folder provisioning: ahead of each new reporting cycle, the backend
automatically creates next month's upload folders (including a new year
folder when the year rolls over) under each account. The user should
always find a folder ready without creating it themselves."

This script only wires ``scheduling.drive_provisioning.
run_drive_folder_provisioning`` to a real Postgres connection + real
Google Drive client. All the actual "which period is next" logic lives in
``scheduling.window.next_month_start`` (pure, unit-tested); all the actual
folder-naming convention lives in ``ingestion.sync`` (milestone 3, already
proven, reused verbatim — never a second, independently-maintained path
convention). This script builds none of that itself.

CADENCE CHOICE (this milestone's call to make, per the brief): scheduled
MONTHLY, a few days before month-end, rather than daily. Reasoning: unlike
the routine sync / FX revaluation jobs, next month's folder isn't actually
needed by anyone until next month starts (uploads for a period never
arrive before that period begins) — there's no equivalent "may not be
ready by day 1, keep retrying" concern here. A single monthly run,
comfortably ahead of the rollover, is enough; it's still idempotent
(``find_or_create_folder`` is a get-or-create by name) so re-running it
manually is always safe if ever needed.

INSTALL ON THE DROPLET (NOT done by this milestone — see CLAUDE.md's
Prototype scope and this milestone's explicit out-of-scope note):

    crontab -e

    # Project-Noctrowl: provision next month's Drive upload folders.
    # Runs on the 25th of each month at 05:00 Asia/Jakarta (WIB, UTC+7) —
    # comfortably before month-end, well ahead of when next month's
    # uploads would ever actually start arriving.
    0 5 25 * * cd /opt/project-noctrowl-2 && /opt/project-noctrowl-2/venv/bin/python3 scripts/scheduled_drive_provisioning.py >> /var/log/noctrowl/drive_provisioning.log 2>&1

USAGE (manual/local run, same as any other scripts/ entrypoint):
    python3 scripts/scheduled_drive_provisioning.py

Reads DATABASE_URL and Google Drive credentials from the environment (see
.env.example) — never hardcodes a connection string or credential. This
job needs WRITE access to Drive (folder creation), so it requires the
OAuth credential path (``GOOGLE_OAUTH_TOKEN_PATH``) to be configured — see
``ingestion/drive_client.py``'s module docstring for why the legacy
service-account path can't write to a personal Gmail Drive at all.
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from ingestion.drive_client import DriveClient, DriveCredentialError  # noqa: E402
from ledger.db import get_engine  # noqa: E402
from scheduling.drive_provisioning import run_drive_folder_provisioning  # noqa: E402


def main() -> int:
    root_folder_id = os.environ.get("GOOGLE_DRIVE_ROOT_FOLDER_ID")
    if not root_folder_id:
        print("ERROR: GOOGLE_DRIVE_ROOT_FOLDER_ID is not set — cannot reach Google Drive.", file=sys.stderr)
        return 1

    try:
        drive_client = DriveClient()
    except DriveCredentialError as exc:
        print(f"ERROR: Google Drive credentials are not configured: {exc}", file=sys.stderr)
        return 1

    engine = get_engine()

    result = run_drive_folder_provisioning(engine, drive_client, root_folder_id=root_folder_id)

    print(f"Drive folder provisioning: target period {result.target_period.isoformat()}")
    if not result.paths:
        print("  No active eBay accounts configured — nothing to provision.")
        return 1

    for p in result.paths:
        print(f"  [{p.scope:>12}] {p.scope_name!r} / {p.subfolder} -> folder_id={p.folder_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
