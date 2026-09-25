"""App-level routing tests — routes registered directly on the Flask app
factory (webapp/__init__.py), not owned by any single screen's blueprint.

Covers the bare root path "/" (added 2026-09-24): before the login gate was
removed, nobody ever hit "/" directly since a successful /login always
redirected to url_for("reports.index"). Now that Dotworks links straight to
this app's URL, "/" needs its own route rather than 404ing.
"""
from __future__ import annotations

from flask import url_for


def test_root_redirects_to_reports_index(client, wconn):
    resp = client.get("/")
    assert resp.status_code == 302
    with client.application.test_request_context():
        assert resp.headers["Location"] == url_for("reports.index")


def test_root_redirect_lands_on_working_page(client, wconn):
    resp = client.get("/", follow_redirects=True)
    assert resp.status_code == 200
    # reports.index itself redirects on to reports.revenue (see
    # webapp/reports_bp.py) — following both hops should land there.
    assert resp.request.path == "/reports/revenue"
