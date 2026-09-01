"""Login gate tests."""
from __future__ import annotations


def test_unauthenticated_request_redirects_to_login(client, wtopology):
    resp = client.get("/documents/")
    assert resp.status_code in (301, 302)
    assert "/login" in resp.headers["Location"]


def test_correct_credentials_log_in_and_reach_a_protected_page(client, login_env, wtopology):
    username, password = login_env
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code in (301, 302)
    resp2 = client.get("/documents/", follow_redirects=True)
    assert resp2.status_code == 200


def test_wrong_password_rejected(client, login_env, wtopology):
    username, _ = login_env
    resp = client.post("/login", data={"username": username, "password": "wrong"})
    assert resp.status_code == 401
    resp2 = client.get("/documents/")
    assert resp2.status_code in (301, 302)  # still not logged in


def test_logout_clears_session(logged_in_client, wtopology):
    resp = logged_in_client.get("/documents/")
    assert resp.status_code == 200
    logged_in_client.post("/logout")
    resp2 = logged_in_client.get("/documents/")
    assert resp2.status_code in (301, 302)
