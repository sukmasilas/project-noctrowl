"""Cron entrypoint: the month-end unrealized FX revaluation job.

Implements CLAUDE.md's "Scheduling & triggers" section: "Month-end FX
revaluation runs as its own scheduled job at period close, distinct from
the routine sync. It must always use that specific period's own
end-of-month Kurs Pajak rate."

This script only wires ``scheduling.fx_revaluation.run_fx_revaluation`` to
a real Postgres connection read from the environment. All the actual
period-selection logic lives in ``scheduling.window.closed_period_for``
(pure, unit-tested); all the actual posting logic lives in
``ledger.posting.post_unrealized_fx_revaluation`` (milestone 2, already
proven). This script builds none of that itself. No Google Drive access
needed here at all (unlike the routine sync / folder-provisioning
scripts) — everything this job needs already lives in Postgres.

CADENCE CHOICE (this milestone's call to make, per the brief — CLAUDE.md
doesn't pin down HOW OFTEN, only that it must be "its own scheduled job at
period close"): scheduled DAILY across days 1-7 of each month (the same
H+7 tail CLAUDE.md already describes for the routine sync job), not a
single once-a-month cron line. Reasoning: CLAUDE.md's own wording — "even
though the job may actually execute a few days later during the H+7
revision window" — implies the rate/data this job depends on (the
period-end Kurs Pajak rate, which is manually seeded — see CLAUDE.md's
"Kurs Pajak sourcing for ingestion") might not be available yet on day 1.
This job is fully idempotent (see scheduling/fx_revaluation.py's
IDEMPOTENCY note and the ux_fx_revaluations_wallet_group_period unique
index) and self-limiting (a wallet-group with no seeded rate yet just
skips and logs — it doesn't error the whole run), so running it daily for
a week is strictly more robust than a single fixed day, at negligible
extra cost (each day after the first successful post is a fast, cheap
no-op — no Drive/OCR work happens in this job at all).

INSTALL ON THE DROPLET (NOT done by this milestone — see CLAUDE.md's
Prototype scope and this milestone's explicit out-of-scope note):

    crontab -e

    # Project-Noctrowl: month-end unrealized FX revaluation. Runs daily
    # 03:00 Asia/Jakarta (WIB, UTC+7) on days 1-7 of each month (the H+7
    # tail) — idempotent, so running it 7 times only ever posts once per
    # wallet-group/period (or zero times, if no rate has been seeded yet
    # by then — check the log and seed the rate, then it'll pick up on
    # the next day's run, or re-run this script manually).
    0 3 1-7 * * cd /opt/project-noctrowl-2 && /opt/project-noctrowl-2/venv/bin/python3 scripts/scheduled_fx_revaluation.py >> /var/log/noctrowl/fx_revaluation.log 2>&1

USAGE (manual/local run, same as any other scripts/ entrypoint):
    python3 scripts/scheduled_fx_revaluation.py

Reads DATABASE_URL from the environment (see .env.example) — never
hardcodes a connection string. Exits 0 if every wallet-group's outcome was
'posted', 'no_change', 'already_posted', or 'missing_kurs_pajak_rate'
(this last one is an expected, self-healing "try again once the rate is
seeded" state, not a failure); exits 1 on 'race_lost', 'missing_usd_
reference', or an outcome this script doesn't recognize, or if there are
no active accounts/wallet-groups configured at all. The missing-rate case
is deliberately NOT treated as a hard failure so a normal early-in-the
-month run (before the Kurs Pajak rate has been seeded yet) doesn't spam
cron's failure notification every single day of the week it's expected to
happen — 'missing_usd_reference' is different and IS treated as a hard
failure: unlike a rate that arrives on its own once seeded, a journal line
with no amount_usd_ref (see scheduling.fx_revaluation.
MissingUsdReferenceError) never fixes itself just by the job running
again later — it needs a human to actually find and backfill the
offending line(s), so this should page/notify, not go quiet.
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

import ingestion.schema  # noqa: E402,F401 - registers kurs_pajak_rates on the shared metadata
from ledger.db import get_engine  # noqa: E402
from scheduling.fx_revaluation import run_fx_revaluation  # noqa: E402

_NON_FAILING_STATUSES = {"posted", "no_change", "already_posted", "missing_kurs_pajak_rate", "missing_payoneer_wallet"}


def main() -> int:
    engine = get_engine()

    result = run_fx_revaluation(engine)

    print(f"FX revaluation: closed period {result.period_month.isoformat()} (rate as of {result.period_end.isoformat()})")

    if not result.outcomes:
        print("  No active eBay accounts/wallet-groups configured — nothing to revalue.")
        return 1

    exit_code = 0
    for outcome in result.outcomes:
        print(
            f"  [{outcome.status:>24}] wallet_group_id={outcome.wallet_group_id} "
            f"journal_entry_id={outcome.journal_entry_id} {outcome.detail or ''}".rstrip()
        )
        if outcome.status not in _NON_FAILING_STATUSES:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
