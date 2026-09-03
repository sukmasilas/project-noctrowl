# Scheduling (Milestone 5)

This is the deployment reference for Milestone 5's three scheduled jobs. It documents the **exact cron configuration a human would install on the droplet** — nothing here has actually been installed on the droplet or on any machine. Per CLAUDE.md's Prototype scope and this milestone's explicit brief, droplet deployment is a separate, later, explicitly-approved step. Everything below was proven locally: unit tests for the pure date logic, and integration tests against a disposable Postgres instance with faked Drive clients (see `tests/scheduling/`).

## What exists

| Job | Module (the logic) | Script (the cron entrypoint) | What it wraps |
|---|---|---|---|
| Routine sync | `scheduling/routine_sync.py` | `scripts/scheduled_routine_sync.py` | `ingestion.sync.run_sync_for_period` (Milestone 3, already proven) |
| Month-end unrealized FX revaluation | `scheduling/fx_revaluation.py` | `scripts/scheduled_fx_revaluation.py` | `ledger.posting.post_unrealized_fx_revaluation` (Milestone 2, already proven) |
| Drive folder provisioning | `scheduling/drive_provisioning.py` | `scripts/scheduled_drive_provisioning.py` | `ingestion.drive_client.DriveClient.find_or_create_folder` (already idempotent) |

None of these three modules implement new ingestion, posting, or matching logic — they only decide **when** the already-tested logic from earlier milestones runs automatically. The actual date/window arithmetic (the H-15/H+7 active window, "which period just closed", "what's next month") is centralized in `scheduling/window.py`, which is pure and has no I/O — see `tests/scheduling/test_window.py` for its boundary-day/year-rollover/leap-year coverage.

## Mechanism: system cron, not an in-process scheduler

All three jobs are plain scripts invoked by system cron (same pattern already established by `scripts/run_migrations.py` and `scripts/authorize_google_drive.py`), **not** an in-process scheduler (e.g. APScheduler) running inside the Flask process.

Why, given the droplet's constraints (1 vCPU / 1GB RAM — see CLAUDE.md's Architecture section, already flagged as tight once Postgres + the web app + OCR workloads run together):

- **Isolation from the web app.** Routine sync does OCR-heavy, potentially slow work (bank/invoice PDF parsing, Drive downloads). If that ran inside the same process as the always-on Flask app, a slow or memory-heavy run could degrade web app responsiveness for whoever's using it at the time. A separate cron-invoked process can't do that — it has its own memory space, and if it dies (OOM-killed, an unhandled exception), the web app is completely unaffected.
- **Simplicity of failure handling.** A cron job's failure mode is just a non-zero exit code and stderr — visible via cron's own mail/log redirection, `journalctl`, or whatever monitoring wraps it. An in-process scheduler thread failing silently inside a long-running Flask worker is a harder failure mode to detect and recover from on a memory-constrained box that's also already running the one thing (the web app) users actually interact with directly.
- **No new runtime dependency.** Cron already exists on the droplet's Ubuntu 24.04 image. An in-process scheduler would be a new library dependency (see CLAUDE.md's QA note on scanning new dependencies) for a problem cron already solves.

## Cron lines to install (later, on the droplet — not installed by this milestone)

Adjust the repo/venv paths and log directory to match the real deployment; the schedule and reasoning below are the actual recommendation.

```cron
# Project-Noctrowl — Milestone 5 scheduled jobs
# Times are droplet-local; the droplet is in the Singapore region — set its
# timezone to Asia/Jakarta (WIB, UTC+7) to match the business, or adjust
# these hours if it stays on a different TZ.

# Routine sync: daily. The job itself no-ops (see scheduling/window.py)
# outside the H-15/H+7 active window CLAUDE.md defines, so a daily cron line
# is correct — an imprecise cron time only affects how promptly a new
# upload gets picked up, never correctness.
0 2 * * * cd /opt/project-noctrowl-2 && /opt/project-noctrowl-2/venv/bin/python3 scripts/scheduled_routine_sync.py >> /var/log/noctrowl/routine_sync.log 2>&1

# Month-end unrealized FX revaluation: daily across days 1-7 of each month
# (the same H+7 tail CLAUDE.md already describes for routine sync). The
# job is fully idempotent (see scheduling/fx_revaluation.py's IDEMPOTENCY
# note and ledger/schema.py's ux_fx_revaluations_wallet_group_period unique
# index) and self-limiting (a wallet-group with no seeded Kurs Pajak rate
# yet just skips and logs, it doesn't fail the whole run) — running it
# daily for a week is more robust than a single fixed day, since the rate
# is manually seeded and might not be available yet on day 1, at
# negligible extra cost (every day after the first successful post is a
# fast, cheap no-op; this job never touches Drive/OCR at all).
0 3 1-7 * * cd /opt/project-noctrowl-2 && /opt/project-noctrowl-2/venv/bin/python3 scripts/scheduled_fx_revaluation.py >> /var/log/noctrowl/fx_revaluation.log 2>&1

# Drive folder provisioning: monthly, a few days before month-end. Unlike
# the two jobs above, next month's folder isn't needed by anyone until
# next month actually starts — there's no "might not be ready yet, keep
# retrying" concern, so a single monthly run is enough. Still idempotent
# (find_or_create_folder is a get-or-create by name), so re-running it
# manually is always safe if ever needed.
0 5 25 * * cd /opt/project-noctrowl-2 && /opt/project-noctrowl-2/venv/bin/python3 scripts/scheduled_drive_provisioning.py >> /var/log/noctrowl/drive_provisioning.log 2>&1
```

Install with `crontab -e` (run as whichever user should own these jobs — never root unless that's already the established convention for this droplet). Create `/var/log/noctrowl/` first (`mkdir -p /var/log/noctrowl`) with write access for that user.

Each script reads `DATABASE_URL` (all three) and, for the routine sync and Drive provisioning scripts only, Google Drive credentials (`GOOGLE_DRIVE_ROOT_FOLDER_ID` + either `GOOGLE_OAUTH_TOKEN_PATH` or the legacy service-account path) from the environment via `python-dotenv`, exactly like every other `scripts/` entrypoint in this project — see `.env.example`. Nothing is hardcoded. The FX revaluation script needs no Drive access at all.

## Exit codes (for cron's own failure detection)

- **`scripts/scheduled_routine_sync.py`**: `0` on a clean no-op (outside the active window) or if every account/period synced without error; `1` if configuration is missing, or at least one account/period's sync raised an unhandled exception (see the `error` status line printed per account/period).
- **`scripts/scheduled_fx_revaluation.py`**: `0` for every "expected" outcome, including `missing_kurs_pajak_rate` (a normal, self-healing "try again once the rate is seeded" state during the first few days of the H+7 tail) — deliberately not treated as a failure so a normal early-month run doesn't spam cron's failure notification every day of the week it's expected to happen. `1` only for a genuinely unexpected outcome (`race_lost`, or zero active accounts/wallet-groups configured at all).
- **`scripts/scheduled_drive_provisioning.py`**: `0` on success; `1` if Drive credentials/root folder aren't configured, or there are no active accounts to provision for.

## What was deliberately NOT built in this milestone

- No droplet crontab was installed — see the top of this document.
- No change to the manual "Sync Now" button or its cooldown (`webapp/documents_bp.py`, `webapp/sync_cooldown.py`) — those were already built in Milestone 4 and are untouched.
- No eBay API sync — still deferred indefinitely, unrelated to scheduling.
- No inter-account-transfer elimination logic — still explicitly out of scope for the one-account prototype; the scheduling logic above is written generically against every *active* eBay account/wallet-group (`webapp.scoping.list_ebay_accounts`) so it needs no rework once a 2nd/3rd account comes online, but it doesn't build the elimination logic itself.
