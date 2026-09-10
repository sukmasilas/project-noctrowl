"""Seed data for milestone-3 ingestion tables — the ``ingestion``-side
counterpart to ``ledger.seed`` (which owns the chart of accounts,
categories, and consignor payout tiers). Kept as a separate module because
``ledger`` never imports from ``ingestion`` (dependency runs the other way:
``ingestion`` already imports ``ledger``), so a table that lives in
``ingestion/schema.py`` gets its seed function here, not folded into
``ledger.seed.seed_catalogs``.

``seed_bank_keyword_rules`` backs auto-match rule (e) — see
``ingestion.matching._try_rule_e_keyword`` and CLAUDE.md's "Bank transaction
classification" section. Every row here was confirmed against real bank
statement / Payoneer export text (``sample-documents/``), not guessed — see
each entry's inline note for exactly which real document and how many times
it was observed.
"""
from __future__ import annotations

from sqlalchemy.engine import Connection

from ingestion.schema import bank_keyword_rules

# (keyword, category, expense_account_type_code, note)
#
# Rule (e) matching is case-insensitive SUBSTRING containment (see
# ingestion/matching.py's _try_rule_e_keyword) — a keyword matches any raw
# bank/Payoneer line description that CONTAINS it, not just an exact line.
# Confirmed 2026-09-02 against the real 4-month sample set
# (sample-documents/Main Account (BCA)/ and sample-documents/Bridging
# Account (Mandiri)/, May-Aug 2026, plus a real Payoneer "Reports &
# Statements" CSV export) that SOME keywords below ALSO correctly recognize
# the equivalent real line on whichever OTHER statement happens to carry the
# same recurring charge/credit — e.g. "BUNGA" matches both BCA Main's own
# "BUNGA" line AND the Bridging (Mandiri) statement's differently-worded
# "Bunga rekening" line (both genuinely contain the substring "Bunga",
# case-insensitively), and "Biaya transfer BI Fast" / "Biaya administrasi
# rekening" are Bridging-statement lines that would equally match a
# BCA-formatted bridging statement carrying the same wording. This is
# intentional, not an accident: rule (e) has no per-statement scoping
# (bank_keyword_rules has no scope column, by design — a keyword rule is a
# recurring-description pattern, not tied to one specific account/document),
# and the underlying real-world fact (this IS bank-credited interest / this
# IS a BI-Fast transfer fee, regardless of which of the business's bank
# accounts it happened on) is the same either way. Flagged explicitly here
# because it means MORE real transactions auto-match than a naive "each
# keyword only ever appears in exactly its own named source" reading of
# Main-agent's brief would suggest.
#
# CORRECTION (QA, 2026-09-02): "PAJAK BUNGA" is NOT one of the
# cross-statement matches above — see that entry's own note below. Only
# "BUNGA" actually has a same-substring counterpart on the Bridging
# statement; "PAJAK BUNGA" and Bridging's "Pajak rekening" share no
# substring at all, so that Bridging line correctly falls to Needs Review,
# not an auto-match. Nothing posts incorrectly because of this (a
# Needs-Review row never auto-posts, and Needs Review is the SAFE default —
# see CLAUDE.md's Bank transaction classification rules), it was just an
# inaccurate claim in this comment.
BANK_KEYWORD_RULES = [
    (
        "Biaya transfer BI Fast",
        "operating_expense",
        "GENERAL_OPEX",
        "BI Fast transfer fee — Bridging (Mandiri) statement, -Rp2,500 each, "
        "~12 occurrences across May-Aug 2026.",
    ),
    (
        "Biaya administrasi rekening",
        "operating_expense",
        "GENERAL_OPEX",
        "Account admin fee — Bridging (Mandiri) statement, -Rp6,000 each, "
        "~4 occurrences (once per month, May-Aug 2026). Deliberately does "
        "NOT also match the Bridging statement's OTHER, textually distinct "
        "'Biaya administrasi kartu debit' (debit-card admin fee) lines — a "
        "genuinely different real fee type, left unmatched/Needs Review, "
        "not in this task's scope.",
    ),
    (
        "BUNGA",
        "interest_income",
        None,  # not used for interest_income — see _post_one_row, always INTEREST_INCOME
        "Bank-credited interest — BCA Main statement's exact 'BUNGA' line "
        "(~4x, once per month) and the Bridging statement's 'Bunga rekening' "
        "line (~4x) both contain this substring; posts as an INFLOW "
        "(credit INTEREST_INCOME) via post_interest_income_line's sign check.",
    ),
    (
        "PAJAK BUNGA",
        "interest_income",
        None,
        "Withholding tax on that interest — matches BCA Main's exact "
        "'PAJAK BUNGA' line only (~4x, once per month, May-Aug 2026); posts "
        "as an OUTFLOW (debit INTEREST_INCOME) via "
        "post_interest_income_line's sign check, so the account's own "
        "balance nets to true net interest received without a separate "
        "tax-expense line. CORRECTED 2026-09-02 (QA finding): this keyword "
        "does NOT also match the Bridging (Mandiri) statement's 'Pajak "
        "rekening' line — the two strings share no common substring ('PAJAK "
        "BUNGA' never appears in the Bridging statement's text at all), "
        "unlike 'BUNGA'/'Bunga rekening' above, which genuinely do share a "
        "substring. 'Pajak rekening' also only appears ONCE across the real "
        "4-month sample (May 2026 only), not ~4x as this note previously, "
        "inaccurately, claimed. That single Bridging 'Pajak rekening' line "
        "correctly falls to Needs Review (no keyword rule matches it) — "
        "nothing posts incorrectly, this was purely a documentation error.",
    ),
    (
        "OPENAI *CHATGPT SUBSCR",
        "operating_expense",
        "GENERAL_OPEX",
        "Recurring Payoneer card charge (raw Payoneer CSV description: "
        "'Card charge (OPENAI *CHATGPT SUBSCR)'), same GENERAL_OPEX "
        "treatment as the DigitalOcean/Namecheap Operations invoices — "
        "confirmed twice (a duplicate charge) in the real Aug 2026 Payoneer "
        "'Reports & Statements' export.",
    ),
    (
        "KURASI",
        "shipping_cost",
        None,  # not used for shipping_cost — see _post_one_row, always SHIPPING_COST
        "Added 2026-09-09 (confirmed directly by the user): Kurasi is a real "
        "shipping vendor this business uses — every bank line whose raw "
        "description contains 'KURASI' is a shipping cost, no exceptions. "
        "Auto-matches straight to the dedicated 'shipping_cost' review-queue "
        "category (posts to SHIPPING_COST, never GENERAL_OPEX — see "
        "_post_one_row's shipping_cost branch), same pattern as "
        "'BUNGA'/'PAJAK BUNGA' above (a hardcoded posting account, not one "
        "looked up from this row's expense_account_type_code column). "
        "Confirmed against the real dev database: 46 real Master Account "
        "bank lines matched 'raw_description ILIKE %kurasi%' (Rp "
        "512,000-2,638,000 each), all still needs_review/unposted before "
        "this fix — see scripts/relabel_kurasi_shipping_cost.py for the "
        "one-off correction of those already-staged rows; this keyword rule "
        "only affects rows staged AFTER this fix.",
    ),
    (
        "BIAYA ADM",
        "operating_expense",
        "GENERAL_OPEX",
        "Added 2026-09-10 (real gap fix): the real BCA Main Account "
        "statement's own bank-admin-fee line is literally 'BIAYA ADM' (May "
        "2026) or 'BIAYA ADM 0998' (Jun-Aug 2026) — Rp 10,000 each, once a "
        "month — SHORTER than, and never matched by, the existing 'Biaya "
        "administrasi rekening' keyword above (that keyword is longer than "
        "the whole description it needs to match against). Relies on "
        "ingestion.matching._keyword_matches's word-boundary-at-the-end "
        "refinement to correctly match both real Master-statement forms "
        "WITHOUT also matching the textually similar but genuinely "
        "different Bridging (Mandiri) 'Biaya administrasi rekening'/'Biaya "
        "administrasi kartu debit' lines (both immediately followed by the "
        "letter 'I', not a space/digit/end-of-string) — see that function's "
        "docstring for the full explanation. Confirmed against the real dev "
        "database: 3 real Master Account rows (May/Jun/Jul 2026) were "
        "sitting needs_review/uncategorized before this fix (Aug's was "
        "already manually labeled and posted).",
    ),
]


def seed_bank_keyword_rules(conn: Connection) -> None:
    """Insert the confirmed recurring-description keyword rules backing
    auto-match rule (e). Safe to call once per fresh schema — like every
    other seed function in this project, this is a plain INSERT with no
    upsert/idempotency guard of its own (schema is dropped/recreated between
    test runs; a real admin-editable screen for this table is milestone
    4/5+ territory, not built here — see CLAUDE.md's Architecture section on
    the Consignor Payout Tiers screen being the analogous future UI for a
    sibling table).
    """
    for keyword, category, expense_account_type_code, _note in BANK_KEYWORD_RULES:
        conn.execute(
            bank_keyword_rules.insert().values(
                keyword=keyword,
                category=category,
                expense_account_type_code=expense_account_type_code,
                is_active=True,
            )
        )
