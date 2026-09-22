"""Flask app factory.

See docs/design/milestone-4-web-app-design.md for the framework choice and
overall architecture. Nothing here defines its own DB engine/connection
concept, its own ORM models, or a second Drive-auth path — see webapp/db.py
and webapp/documents_bp.py for how the existing ledger/ingestion packages
are reused directly.

No login gate here anymore (removed 2026-09-22) — see CLAUDE.md's Architecture
section, "Login gate — superseded 2026-09-22": a separate project, Dotworks,
is now the single shared login/entry point in front of this app (and
Project-Alakazam), so every route below is reachable directly with no
authentication of its own. ``SECRET_KEY`` is still required — Flask's
``flash()`` (used e.g. by webapp/review_queue_bp.py) needs a signed session
to store the flashed message across the redirect, independent of login.
"""
from __future__ import annotations

import os

from flask import Flask
from sqlalchemy.engine import Engine


def create_app(*, engine: Engine | None = None, drive_client=None) -> Flask:
    app = Flask(__name__)

    secret_key = os.environ.get("APP_SECRET_KEY")
    if not secret_key:
        raise RuntimeError(
            "APP_SECRET_KEY is not set. Copy .env.example to .env and generate one "
            "(see the comment there) — never hardcode a session-signing key."
        )
    app.config["SECRET_KEY"] = secret_key
    app.config["DRIVE_CLIENT"] = drive_client

    from webapp import db as db_module

    db_module.init_app(app, engine=engine)

    from webapp.bank_reconciliation_bp import bp as bank_reconciliation_bp
    from webapp.documents_bp import bp as documents_bp
    from webapp.general_ledger_bp import bp as general_ledger_bp
    from webapp.journal_entries_bp import bp as journal_entries_bp
    from webapp.report_extras import register_template_filters
    from webapp.reports_bp import bp as reports_bp
    from webapp.review_queue_bp import bp as review_queue_bp
    from webapp.settings_bp import bp as settings_bp
    from webapp.subsidiary_ledger_bp import bp as subsidiary_ledger_bp
    from webapp.wallet_bp import bp as wallet_bp

    app.register_blueprint(documents_bp)
    app.register_blueprint(review_queue_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(bank_reconciliation_bp)
    app.register_blueprint(wallet_bp)
    app.register_blueprint(journal_entries_bp)
    app.register_blueprint(general_ledger_bp)
    app.register_blueprint(subsidiary_ledger_bp)

    register_template_filters(app)

    return app
