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

from flask import Flask, redirect, url_for
from sqlalchemy.engine import Engine


class PrefixMiddleware:
    """WSGI middleware making Flask (and thus ``url_for()``) aware of a
    path prefix stripped by an upstream reverse proxy (added 2026-09-25).

    Project-Noctrowl is deployed behind nginx as part of a combined site:
    ``dotworks.net`` is served by a sibling app (Dotworks), and nginx proxies
    ``dotworks.net/noctrowl/*`` through to this Flask app on an internal-only
    port, stripping the ``/noctrowl`` prefix before forwarding — so from this
    app's own point of view, a request for ``dotworks.net/noctrowl/reports``
    arrives looking like a plain request for ``/reports``. Without this
    middleware, ``url_for()`` (nav links, redirects, static asset URLs) would
    generate paths starting from ``/`` with no idea a ``/noctrowl`` prefix
    exists on the outside, breaking every link/redirect/asset reference once
    deployed this way.

    nginx is expected to set the ``X-Script-Name`` header to the prefix it
    stripped (e.g. ``/noctrowl``). This middleware reads that header and, if
    present, sets ``environ['SCRIPT_NAME']`` to it (which is what
    ``url_for()`` and Flask's routing consult) and strips that same prefix
    off the front of ``environ['PATH_INFO']`` if it's there (since nginx's
    proxied request may still include the full original path depending on
    proxy config — stripping defensively here keeps routing correct either
    way).

    This is a standard, well-known pattern for mounting a Flask app under a
    prefix behind a reverse proxy — not a Project-Noctrowl invention. It is a
    distinct concern from Werkzeug's ``ProxyFix`` (which handles
    client-IP/scheme headers like ``X-Forwarded-For``/``X-Forwarded-Proto``,
    not prefix stripping) — no new dependency is needed for this.

    Critically, when the ``X-Script-Name`` header is absent (e.g. local dev
    via ``flask run``, or any request not passing through the reverse proxy),
    this is a complete no-op — ``environ`` is passed through unchanged. This
    must never affect local dev or the current already-deployed-nowhere-yet
    behavior.
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        script_name = environ.get("HTTP_X_SCRIPT_NAME", "")
        if script_name:
            environ["SCRIPT_NAME"] = script_name
            path_info = environ.get("PATH_INFO", "")
            if path_info.startswith(script_name):
                environ["PATH_INFO"] = path_info[len(script_name):]
        return self.wsgi_app(environ, start_response)


def create_app(*, engine: Engine | None = None, drive_client=None) -> Flask:
    app = Flask(__name__)
    app.wsgi_app = PrefixMiddleware(app.wsgi_app)  # type: ignore[method-assign]

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

    # Bare root path (added 2026-09-24). Before the login gate was removed
    # (see the module docstring above), nobody ever hit "/" directly — a
    # successful /login always redirected to url_for("reports.index").
    # Dotworks now links straight to this app's URL, so "/" needs its own
    # route rather than 404ing. Reuses the same default target the old login
    # flow used, registered on the app directly (not owned by any one
    # blueprint) since it's app-level routing.
    @app.route("/")
    def root():
        return redirect(url_for("reports.index"))

    return app
