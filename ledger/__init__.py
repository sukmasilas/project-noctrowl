"""Project-Noctrowl core ledger engine (milestone 2).

Postgres schema + double-entry posting engine implementing this business's
money-math rules (COGS timing, consignment liability accrual, inter-account
transfers, FX realized/unrealized split). See CLAUDE.md at the repo root for
the full accounting spec this package implements.

Explicitly out of scope for this package (see CLAUDE.md Build milestones):
Google Drive ingestion, CSV/PDF parsing, review-queue auto-match logic, any
web UI/HTTP layer, eBay API sync, tax logic, scheduling.
"""
