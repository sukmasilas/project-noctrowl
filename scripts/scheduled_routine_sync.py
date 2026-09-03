"""Cron entrypoint: the automatic routine sync job.

Implements CLAUDE.md's "Scheduling & triggers" section: "Routine sync job
... runs daily, but only within an active window around each month's
close: from 15 days before month-end through 7 days after month-end."

This script only wires ``scheduling.routine_sync.run_routine_sync`` to a
real Postgres connection + real Google Drive client, both read from the
environment (via python-dotenv, same pattern as every other ``scripts/``
entrypoint in this project — ``scripts/run_migrations.py``,
``scripts/authorize_google_drive.py``). All the actual window/period logic
lives in ``scheduling.window`` (pure, unit-tested); all the actual
sync/parse/match/post logic lives in ``ingestion.sync`` (milestone 3,
already proven). This script builds none of that itself.

The job harmlessly no-ops on any day outside the H-15/H+7 active window —
see ``scheduling/window.py``. That's what makes a daily cron schedule
correct here: the job decides for itself whether there's anything to do
today, so an imprecise cron time is only a "how promptly is a new upload
picked up" concern, never a correctness risk.

INSTALL ON THE DROPLET (NOT done by this milestone — see CLAUDE.md's
Prototype scope and this milestone's explicit out-of-scope note; this is
documentation for a LATER, explicitly-approved deployment step, not
something run against this machine or the droplet by this script):

    crontab -e

    # Project-Noctrowl: routine sync (daily; no-ops outside the H-15/H+7
    # window). Runs at 02:00 Asia/Jakarta (WIB, UTC+7) — outside typical
    # business hours. Adjust the venv/repo paths to match the real
    # droplet deployment.
    0 2 * * * cd /opt/project-noctrowl-2 && /opt/project-noctrowl-2/venv/bin/python3 scripts/scheduled_routine_sync.py >> /var/log/noctrowl/routine_sync.log 2>&1

USAGE (manual/local run, same as any other scripts/ entrypoint):
    python3 scripts/scheduled_routine_sync.py

Reads DATABASE_URL and Google Drive credentials from the environment (see
.env.example) — never hardcodes a connection string or credential. Exits
0 on a clean run (including a clean no-op outside the active window) or
if every account/period synced without error; exits 1 if configuration is
missing, or if at least one account/period failed (see the per-outcome
status lines printed to stdout) — non-zero so cron's own failure
notification (mail, or whatever monitoring wraps this) fires correctly.
"""
from __future__ import annotations

import logging
import os
import sys

# Allow this script to be run directly (e.g. `python3 scripts/scheduled_routine_sync.py`
# from the project root) — sys.path[0] is otherwise this file's own directory
# (scripts/), not the project root, so the top-level `ingestion`/`ledger`/
# `scheduling`/`webapp` packages wouldn't be importable. Same fix as every
# other scripts/ entrypoint in this project.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

import ingestion.schema  # noqa: E402,F401 - registers milestone-3 tables (kurs_pajak_rates, review_queue, etc.)
from ingestion.drive_client import DriveClient, DriveCredentialError  # noqa: E402
from ledger.db import get_engine  # noqa: E402
from scheduling.routine_sync import run_routine_sync  # noqa: E402


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

    result = run_routine_sync(engine, drive_client, root_folder_id=root_folder_id)

    if not result.active:
        print(f"Routine sync: outside the active window today ({result.ran_at.isoformat()}) — no-op.")
        return 0

    print(f"Routine sync: active for period(s) {[p.isoformat() for p in result.periods]}")
    exit_code = 0
    for outcome in result.outcomes:
        posted = outcome.result.posted.posted if (outcome.result and outcome.result.posted) else 0
        print(
            f"  [{outcome.status:>22}] account={outcome.ebay_account_name!r} "
            f"period={outcome.period_month.isoformat()} posted={posted} {outcome.detail or ''}".rstrip()
        )
        if outcome.status == "error":
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
