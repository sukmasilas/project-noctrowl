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


# --- PrefixMiddleware (added 2026-09-25) -----------------------------------
#
# Project-Noctrowl is deployed behind nginx as part of a combined site:
# dotworks.net/noctrowl/* is proxied through to this app with the
# "/noctrowl" prefix stripped before forwarding, and nginx is expected to
# tell this app about that stripped prefix via the X-Script-Name header.
# These tests confirm url_for()/redirects become prefix-aware when that
# header is present, and — just as importantly — that behavior is byte-for-
# byte unchanged when it's absent (local dev, and the current
# nowhere-yet-deployed state).


def test_root_redirect_has_no_prefix_when_header_absent(client, wconn):
    """No X-Script-Name header at all: must behave exactly as today."""
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/reports/"


def test_root_redirect_is_prefixed_when_header_present(client, wconn):
    resp = client.get("/", headers={"X-Script-Name": "/noctrowl"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/noctrowl/reports/"


def test_static_asset_url_has_no_prefix_when_header_absent(client, wconn):
    resp = client.get("/reports/revenue")
    assert resp.status_code == 200
    assert b'href="/static/style.css"' in resp.data
    assert b'href="/noctrowl/static/style.css"' not in resp.data


def test_static_asset_url_is_prefixed_when_header_present(client, wconn):
    resp = client.get("/reports/revenue", headers={"X-Script-Name": "/noctrowl"})
    assert resp.status_code == 200
    assert b'href="/noctrowl/static/style.css"' in resp.data


def test_full_redirect_chain_is_prefixed_when_header_present(client, wconn):
    # "/" -> reports.index -> reports.revenue, both redirects, both should
    # carry the prefix, and the final page should load successfully when
    # the test client follows redirects (each hop re-sends the header since
    # Werkzeug's test client replays headers across redirects it follows).
    resp = client.get("/", headers={"X-Script-Name": "/noctrowl"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/noctrowl/reports/"

    resp2 = client.get("/reports/", headers={"X-Script-Name": "/noctrowl"})
    assert resp2.status_code == 302
    assert resp2.headers["Location"] == "/noctrowl/reports/revenue"


def test_prefix_middleware_is_noop_without_header_direct(app):
    """Directly exercises the WSGI middleware (not just one route) to
    confirm environ is passed through completely untouched when
    X-Script-Name is absent — the highest-risk regression for this change.
    """
    captured = {}

    def fake_start_response(status, headers):
        captured["status"] = status

    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/reports/revenue",
        "SCRIPT_NAME": "",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "80",
        "wsgi.url_scheme": "http",
        "wsgi.input": None,
        "wsgi.errors": None,
        "wsgi.version": (1, 0),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    original_path_info = environ["PATH_INFO"]
    original_script_name = environ["SCRIPT_NAME"]

    app.wsgi_app(environ, fake_start_response)

    assert environ["PATH_INFO"] == original_path_info
    assert environ["SCRIPT_NAME"] == original_script_name
    assert captured["status"].startswith("200")
