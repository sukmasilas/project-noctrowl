"""Flask app factory.

See docs/design/milestone-4-web-app-design.md for the framework choice and
overall architecture. Nothing here defines its own DB engine/connection
concept, its own ORM models, or a second Drive-auth path — see webapp/db.py
and webapp/documents_bp.py for how the existing ledger/ingestion packages
are reused directly.
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

    from webapp.auth import bp as auth_bp
    from webapp.documents_bp import bp as documents_bp
    from webapp.report_extras import register_template_filters
    from webapp.reports_bp import bp as reports_bp
    from webapp.review_queue_bp import bp as review_queue_bp
    from webapp.settings_bp import bp as settings_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(review_queue_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(reports_bp)

    register_template_filters(app)

    @app.before_request
    def _require_login():
        from flask import request, session

        from webapp.auth import SESSION_KEY

        exempt_endpoints = {"auth.login", "static"}
        if request.endpoint in exempt_endpoints or request.endpoint is None:
            return None
        if not session.get(SESSION_KEY):
            from flask import redirect, url_for

            return redirect(url_for("auth.login", next=request.path))
        return None

    return app
