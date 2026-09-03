# Project-Noctrowl

Bookkeeping & financial reporting system for a multi-account eBay reselling business (TCG cards, luxury watches, automotive parts), based in Indonesia, books kept in IDR.

This is a purpose-built tool for one business's actual workflow — not a general accounting SaaS. Full business rules, accounting logic, and build process live in [`CLAUDE.md`](./CLAUDE.md); this file is the practical "what is this / how do I run it" overview.

## Status

**Current milestone: 1 — UI/UX design pass.** Design-led on purpose: sketch every screen and flow before any backend exists, so the sketch tells us what to build rather than the other way around. No backend, no database, no integrations yet — see [`docs/design/`](./docs/design/) for the design spec and a clickable static mockup. See [Build milestones](./CLAUDE.md#build-milestones-step-by-step) in CLAUDE.md for the full sequence.

## What it does (once fully built)

- Ingests eBay sales data (3 accounts, mixed categories), bank statements (PDF), Payoneer exports (CSV), and invoices — all via manual upload to Google Drive for now. **eBay API sync is deferred** (2026-08-31): the prototype runs fully manual first, with live API integration designed as its own step later.
- Posts every transaction to a double-entry ledger, applying this business's specific rules: COGS timing per sales model (stock/pre-order/consignment), consignment payable accrual with tiered payout rates, inter-account transfer handling, and FX gain/loss split into realized (at Payoneer withdrawal) vs. unrealized (month-end revaluation).
- Auto-matches uploaded documents against expected transactions, flags anything it can't confidently classify in a review queue, and surfaces missing/overdue documents on a Documents screen so a quiet month doesn't get mistaken for a fully-reviewed one.
- Serves read-only reports (Revenue & Cash Flow per account + consolidated; P&L & Equity consolidated only) from a web app, marked Provisional until all review-queue items are cleared and all expected documents have arrived.

## Architecture at a glance

- **Backend:** Python, running on a DigitalOcean droplet (Ubuntu 24.04, Singapore).
- **Database:** PostgreSQL — single source of truth for the ledger, review queue, and reports.
- **File storage:** Google Drive, used only as plain storage for uploaded bank statements/invoices/CSVs (no Sheets API).
- **Interface:** a self-hosted web app (review queue + report views), view-only for reports (no export/download, by design). No framework chosen yet — that's decided once we reach implementation.

See [`docs/flowcharts.md`](./docs/flowcharts.md) for diagrams of both the business money flow and the system's data pipeline.

## Why this replaced the earlier version

An earlier build of this project (`project-noctrowl`) used Google Sheets, via the Sheets API, as the entire interface and backing store. That turned out to be too much integration overhead for too little benefit, so it was scrapped in favor of a standalone app with a real database — built incrementally so each layer (design, then ledger logic, then manual data ingestion, then the UI, then scheduling) is verified before the next one is added. eBay API sync was also pulled out of the near-term plan (2026-08-31) — it's a separate future step once the manual pipeline is proven, not a milestone-3 dependency.

## Setup

Not yet applicable — milestone 1 (design) has no dependencies at all; you can open `docs/design/mockup.html` directly in a browser, nothing to install. Even milestone 2 (ledger engine) only needs Postgres and Python, no external credentials. eBay/Google/droplet credential setup will be documented here once milestone 3 starts and those integrations are actually wired up. Credentials always live in `.env` / `secrets/` (gitignored), never hardcoded — see `CLAUDE.md`'s Report finalization status section for the full rule.

## Repo layout

```
project-noctrowl-2/
├── CLAUDE.md              — full spec: business rules, accounting logic, architecture, agent workflow
├── README.md              — this file
└── docs/
    ├── flowcharts.md      — money flow + system data flow diagrams
    ├── scheduling.md      — milestone 5: the three scheduled jobs, cron config to install later, why cron over an in-process scheduler
    └── design/
        ├── ui-ux-design.md — screens, elements, states, and user flows (milestone 1 spec)
        └── mockup.html      — clickable static mockup, open directly in a browser
```
