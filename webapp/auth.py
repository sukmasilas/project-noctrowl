"""Basic single-shared-login gate.

Per CLAUDE.md's Architecture section: "a basic username/password screen
gates access to everything... Single shared login is sufficient for the
prototype (one owner/user) — no roles/permissions system needed yet."

Credentials come from APP_LOGIN_USERNAME / APP_LOGIN_PASSWORD (env vars,
never hardcoded — see .env.example). Deliberate simplification vs. the
Phase A design doc's "store a pre-hashed password" proposal: this project's
own established convention for every other credential (DATABASE_URL's
password, the Google service-account file) is "plaintext value in a
gitignored .env, read via an env var" — not pre-hashed at rest. Matching
that same convention here (rather than introducing hashing for only this
one credential, which would need its own one-off setup script and a
different mental model from every other secret in this project) keeps the
threat model consistent: whoever can read .env can already read
DATABASE_URL's plaintext password too. The comparison itself still uses
``hmac.compare_digest`` (constant-time) rather than ``==``, so a login
attempt can't be used to time-attack the password character-by-character —
that protection doesn't require the stored value to be hashed.
"""
from __future__ import annotations

import functools
import hmac
import os

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

bp = Blueprint("auth", __name__)

SESSION_KEY = "logged_in"


class LoginNotConfiguredError(RuntimeError):
    """Raised when APP_LOGIN_USERNAME/APP_LOGIN_PASSWORD aren't set."""


def _configured_credentials() -> tuple[str, str]:
    username = os.environ.get("APP_LOGIN_USERNAME")
    password = os.environ.get("APP_LOGIN_PASSWORD")
    if not username or not password:
        raise LoginNotConfiguredError(
            "APP_LOGIN_USERNAME and APP_LOGIN_PASSWORD must both be set (see .env.example) "
            "before the app can serve any request — there is no default/blank login."
        )
    return username, password


def check_credentials(username: str, password: str) -> bool:
    expected_username, expected_password = _configured_credentials()
    # Both comparisons run unconditionally (not short-circuited on the
    # username check) so a wrong username doesn't return faster than a
    # wrong password — avoids leaking which field was wrong via timing.
    username_ok = hmac.compare_digest(username, expected_username)
    password_ok = hmac.compare_digest(password, expected_password)
    return username_ok and password_ok


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get(SESSION_KEY):
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        try:
            valid = check_credentials(username, password)
        except LoginNotConfiguredError:
            current_app.logger.error("Login attempted but APP_LOGIN_USERNAME/PASSWORD are not configured.")
            flash("Login is not configured on this server yet.", "error")
            return render_template("login.html"), 500
        if valid:
            session.clear()
            session[SESSION_KEY] = True
            next_url = request.args.get("next") or url_for("reports.index")
            return redirect(next_url)
        flash("Incorrect username or password.", "error")
        return render_template("login.html"), 401
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
