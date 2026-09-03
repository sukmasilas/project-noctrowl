"""Milestone 5 — scheduling.

Wires the already-proven, manually-callable pipelines from earlier
milestones (``ingestion.sync.run_sync_for_period``, the review-queue
matching engine, ``ledger.posting.post_unrealized_fx_revaluation``) to
*when* they run automatically, per CLAUDE.md's "Scheduling & triggers"
section:

- ``scheduling.window`` — pure, I/O-free date logic (the H-15/H+7 active
  window, month-end FX period selection, "next month" for Drive folder
  provisioning). No DB, no Drive, no implicit ``date.today()`` reads deep
  inside — every function takes ``today`` explicitly, which is what makes
  the boundary logic unit-testable against fixed dates.
- ``scheduling.routine_sync`` — the daily routine sync job (active only
  within the H-15/H+7 window).
- ``scheduling.fx_revaluation`` — the month-end unrealized FX revaluation
  job (a separate scheduled job from routine sync).
- ``scheduling.drive_provisioning`` — automatic month-ahead (and, on a
  year rollover, year-ahead) Google Drive upload-folder provisioning.

Nothing in this package builds new ingestion/posting/matching logic of its
own — it only decides *when* the existing, already-tested logic runs. See
each submodule's docstring for the specific CLAUDE.md section it
implements, and ``scripts/scheduled_*.py`` for the actual cron entrypoints
(this milestone documents the exact crontab lines a human would install on
the droplet later — it does not install anything itself; see CLAUDE.md's
Prototype scope and this milestone's explicit out-of-scope note).
"""
