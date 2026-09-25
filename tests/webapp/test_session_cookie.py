"""Regression coverage for a real, live production incident (2026-09-25):
Project-Noctrowl is deployed on the same domain (dotworks.net) as a separate
sibling app, Dotworks, which owns dotworks.net/login and is the real login
gate in front of both apps (see webapp/__init__.py's module docstring and
CLAUDE.md's "Login gate — superseded 2026-09-22").

Both apps are plain Flask apps. Flask's default SESSION_COOKIE_NAME is the
literal string "session". Noctrowl still uses Flask's session purely for
flash() (see webapp/documents_bp.py's sync_now(), webapp/review_queue_bp.py's
label_row(), webapp/settings_bp.py) — every flash() call writes a
Set-Cookie: session=... header signed with THIS app's APP_SECRET_KEY. Because
that cookie shared both name and path ("/") with Dotworks' own login-session
cookie on the same domain, it silently overwrote the user's Dotworks login
session in their browser — the next request nginx's auth_request check made
against Dotworks then failed (a signature made with a different secret key),
so Dotworks correctly, from its own point of view, treated the user as
logged out and bounced them to /login.

Reproduced live: log into Dotworks -> submit Noctrowl's Sync Now form
(flashes an error since Drive isn't configured) -> next page load redirects
to Dotworks' login page, silently "logging the user out" with no warning.

Fix: webapp/__init__.py's create_app() now sets
app.config["SESSION_COOKIE_NAME"] = "noctrowl_session" — a distinct,
app-namespaced cookie name that can never collide with Dotworks' (or any
other sibling app's, e.g. Alakazam) "session" cookie on the same domain.
"""
from __future__ import annotations

from flask import Flask, flash


def _cookie_names_from_response(resp) -> set[str]:
    """Extract just the cookie NAMEs from every Set-Cookie header on a
    response — e.g. "noctrowl_session=eyJ...; Path=/; HttpOnly" -> {"noctrowl_session"}.
    """
    names = set()
    for header_value in resp.headers.getlist("Set-Cookie"):
        name = header_value.split("=", 1)[0].strip()
        names.add(name)
    return names


def test_sync_now_flash_sets_noctrowl_session_cookie_not_generic_session(client, wtopology, monkeypatch):
    """Reproduces the exact live scenario: Sync Now with Drive unconfigured
    flashes an error, which sets a Set-Cookie. Confirm that cookie is named
    "noctrowl_session", and — critically — that nothing on this response is
    named the bare "session", which is the literal name Dotworks' own login
    cookie uses and the thing that caused the real collision.
    """
    monkeypatch.delenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", raising=False)
    conn, topo = wtopology
    resp = client.post(
        "/documents/sync",
        data={"account_id": str(topo["ebay_account_id"]), "period": "2026-07"},
        follow_redirects=False,
    )
    # The route redirects after flashing (PRG pattern) — the Set-Cookie
    # carrying the flashed message lives on THIS response, not the one
    # after following the redirect.
    assert resp.status_code in (301, 302)

    cookie_names = _cookie_names_from_response(resp)
    assert cookie_names, "Expected at least one Set-Cookie header on the flash-triggering response"
    assert "noctrowl_session" in cookie_names
    assert "session" not in cookie_names


def test_label_row_flash_sets_noctrowl_session_cookie(client, wtopology):
    """Same property, exercised through review_queue_bp.py's label_row() —
    the other real flash() call site named in the bug report — to confirm
    this isn't specific to one blueprint.
    """
    from tests.webapp.conftest import make_source_document
    from tests.webapp.conftest import make_review_queue_row
    import datetime as _dt

    conn, topo = wtopology
    source_doc_id = make_source_document(
        conn,
        document_type="bank_statement_wallet_group",
        period_month=_dt.date(2026, 7, 1),
        wallet_group_id=topo["wallet_group_id"],
        ingested=True,
    )
    row_id = make_review_queue_row(
        conn,
        source_document_id=source_doc_id,
        transaction_date=_dt.date(2026, 7, 15),
    )
    conn.commit()

    # An invalid category deliberately triggers the "Please choose a valid
    # category." error flash() at webapp/review_queue_bp.py:189.
    resp = client.post(
        f"/review-queue/{row_id}",
        data={"category": "not-a-real-category", "period": "2026-07"},
        follow_redirects=False,
    )
    assert resp.status_code in (301, 302)

    cookie_names = _cookie_names_from_response(resp)
    assert cookie_names, "Expected at least one Set-Cookie header on the flash-triggering response"
    assert "noctrowl_session" in cookie_names
    assert "session" not in cookie_names


def test_flash_messages_still_round_trip_and_render(client, wtopology, monkeypatch):
    """Confirms the fix doesn't regress the one thing this session cookie
    actually exists for: a flashed message must still survive the redirect
    and render on the next page (verified working during the login-removal
    work — this just re-confirms it under the renamed cookie).
    """
    monkeypatch.delenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", raising=False)
    conn, topo = wtopology
    resp = client.post(
        "/documents/sync",
        data={"account_id": str(topo["ebay_account_id"]), "period": "2026-07"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"not configured" in resp.data or b"GOOGLE_DRIVE_ROOT_FOLDER_ID" in resp.data


def test_noctrowl_session_cookie_name_is_configured_on_the_app(app):
    """Direct, minimal assertion on the Flask config itself — the simplest
    possible guard against someone reverting this to the Flask default.
    """
    assert app.config["SESSION_COOKIE_NAME"] == "noctrowl_session"


def test_noctrowl_cookie_cannot_collide_with_a_sibling_apps_default_session_cookie(client, wtopology, monkeypatch):
    """End-to-end simulation of the actual collision scenario described in
    the bug report: two separate Flask apps (standing in for Noctrowl and
    Dotworks) both writing Set-Cookie responses into what a real browser
    would treat as one shared cookie jar for the domain (a plain dict keyed
    by cookie name, exactly how a browser's cookie jar dedupes by name).

    Before the fix, both apps used the literal name "session", so applying
    Noctrowl's Set-Cookie second would silently replace Dotworks' entry in
    the jar under the same key — exactly the real incident. After the fix,
    both cookies coexist under distinct keys, so neither app can ever
    clobber the other's session.
    """
    # Stand-in "Dotworks": an unrelated plain Flask app using Flask's
    # untouched default SESSION_COOKIE_NAME ("session"), doing nothing but
    # logging the user in via a session-backed flash-equivalent write.
    dotworks_stub = Flask("dotworks_stub")
    dotworks_stub.config["SECRET_KEY"] = "dotworks-stub-secret"

    @dotworks_stub.route("/login", methods=["POST"])
    def _dotworks_login():
        flash("logged in")
        return "ok"

    dotworks_client = dotworks_stub.test_client()

    # Step 1: user logs into Dotworks -> browser jar gets Dotworks' session cookie.
    dotworks_resp = dotworks_client.post("/login")
    browser_jar: dict[str, str] = {}
    for header_value in dotworks_resp.headers.getlist("Set-Cookie"):
        name, _, rest = header_value.partition("=")
        browser_jar[name.strip()] = rest.split(";", 1)[0]
    assert "session" in browser_jar, "Sanity check: stub Dotworks app must use the plain 'session' cookie name"

    # Step 2: same browser (same domain) triggers a Noctrowl flash().
    monkeypatch.delenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", raising=False)
    conn, topo = wtopology
    noctrowl_resp = client.post(
        "/documents/sync",
        data={"account_id": str(topo["ebay_account_id"]), "period": "2026-07"},
        follow_redirects=False,
    )
    for header_value in noctrowl_resp.headers.getlist("Set-Cookie"):
        name, _, rest = header_value.partition("=")
        browser_jar[name.strip()] = rest.split(";", 1)[0]

    # The critical assertion: Dotworks' "session" cookie must still be
    # present and untouched in the jar — Noctrowl's write must have landed
    # under its own distinct key, not overwritten Dotworks' entry.
    assert "session" in browser_jar
    assert browser_jar["session"] != ""
    assert "noctrowl_session" in browser_jar
    assert browser_jar["session"] != browser_jar["noctrowl_session"]
