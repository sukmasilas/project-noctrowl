"""Project-Noctrowl milestone 3 — manual data ingestion + review queue.

Implements docs/design/milestone-3-ingestion-design.md (Phase A design,
approved by Main-agent 2026-08-31 — see that doc's §0 for the resolved open
questions this package builds against).

This package is purely additive on top of milestone 2's ``ledger`` package:
nothing in ``ledger/schema.py``'s existing tables/constraints or
``ledger/posting.py``'s existing function *behavior* is changed (two
approved additive exceptions, both backward-compatible: new optional kwargs
on ``post_inter_account_transfer``, and a new nullable
``reimbursed_journal_entry_id`` column appended to the existing
``consignment_sales`` table — see ``ingestion/schema.py``).

Covers: eBay Seller Hub Transaction Report CSV parsing, Payoneer CSV +
withdrawal-confirmation-PDF parsing, BCA bank-statement PDF text extraction,
invoice/proof-of-purchase OCR capture, the shared auto-match priority engine
(CLAUDE.md's Bank transaction classification rules a-e), posted-row
idempotency tracking, and a thin Google Drive read-layer interface.

No web UI, no login, no scheduling/cron, no eBay API sync — see CLAUDE.md's
Build milestones for what's explicitly out of scope here.
"""
