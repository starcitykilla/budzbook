"""Tests for BudzBook Terms of Service clickwrap.

Covers: public /terms page, signup checkbox gating (web + staged Twitch
OAuth), the terms_gate enforcement middleware, and POST /terms/accept.
No network calls are made (OAuth network functions are monkeypatched).
"""
import asyncio
import os
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="bb_terms_test_")
os.environ["THC_SOCIAL_DB"] = os.path.join(_tmp, "test.db")
os.environ["THC_SOCIAL_MEDIA"] = os.path.join(_tmp, "media")
os.makedirs(os.path.join(_tmp, "media", "avatars"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "posts"), exist_ok=True)

from fastapi.testclient import TestClient  # noqa: E402

import aiosqlite  # noqa: E402

import app.main as main_mod  # noqa: E402
from app.auth import (AGE_COOKIE, SESSION_COOKIE, make_age_token,  # noqa: E402
                      make_pending_oauth_token, make_session_token,
                      read_pending_oauth_token)
from app.db import DB_PATH, init_db  # noqa: E402
from app.main import TERMS_VERSION, app  # noqa: E402
from app import twitch_oauth  # noqa: E402

asyncio.run(init_db())


def _client():
    c = TestClient(app)
    c.cookies.set(AGE_COOKIE, make_age_token())
    return c


def _insert_user(username, with_terms=False):
    """Insert a user row directly, bypassing /register."""
    async def go():
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            now = "2026-09-17T00:00:00+00:00"
            cur = await db.execute(
                "INSERT INTO users (username, display_name, password_hash,"
                " terms_accepted_at, terms_version, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (username, username.title(), "x",
                 now if with_terms else None,
                 TERMS_VERSION if with_terms else None, now))
            await db.commit()
            return cur.lastrowid
    return asyncio.run(go())


def _login_client(uid):
    c = _client()
    c.cookies.set(SESSION_COOKIE, make_session_token(uid))
    return c


# ---------------------------------------------------------------- /terms
def test_terms_page_is_public_and_contains_key_text():
    c = _client()  # not logged in
    r = c.get("/terms")
    assert r.status_code == 200
    assert "Terms and Conditions" in r.text
    assert "Limitation of liability" in r.text
    assert "Indemnification" in r.text
    assert "21" in r.text
    assert "Blindman Gaming" in r.text


def test_footer_links_to_terms():
    c = _client()
    r = c.get("/")
    assert r.status_code == 200
    assert 'href="/terms"' in r.text


# ---------------------------------------------------------------- web signup gating
def test_register_without_checkbox_is_rejected():
    c = _client()
    r = c.post("/register", data={
        "username": "terms_noagree", "display_name": "No Agree",
        "password": "secret123", "twitch_username": ""},
        follow_redirects=False)
    assert r.status_code == 400
    assert "Terms of Service" in r.text
    # no account was created
    async def check():
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id FROM users WHERE username = ?", ("terms_noagree",))
            return await cur.fetchone()
    assert asyncio.run(check()) is None


def test_register_with_checkbox_stamps_terms():
    c = _client()
    r = c.post("/register", data={
        "username": "terms_agree", "display_name": "Agree",
        "password": "secret123", "twitch_username": "",
        "agree_terms": "yes"},
        follow_redirects=False)
    assert r.status_code == 303
    async def check():
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT terms_accepted_at, terms_version FROM users"
                " WHERE username = ?", ("terms_agree",))
            return await cur.fetchone()
    row = asyncio.run(check())
    assert row["terms_accepted_at"]
    assert row["terms_version"] == TERMS_VERSION


# ---------------------------------------------------------------- middleware + accept
def test_middleware_redirects_unaccepted_user_to_terms():
    uid = _insert_user("terms_gated")
    c = _login_client(uid)
    r = c.get("/feed", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/terms?next=")
    # /terms itself must stay reachable (no redirect loop)
    r2 = c.get("/terms")
    assert r2.status_code == 200
    assert "I have read and agree" in r2.text


def test_accept_requires_checkbox_and_then_unblocks():
    uid = _insert_user("terms_accepter")
    c = _login_client(uid)
    # without the checkbox -> 400, still gated
    r = c.post("/terms/accept", data={"agree": ""}, follow_redirects=False)
    assert r.status_code == 400
    r = c.get("/feed", follow_redirects=False)
    assert r.status_code == 303
    # with the checkbox -> accepted, ?next= honored
    r = c.post("/terms/accept", data={"agree": "yes", "next": "/feed"},
               follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/feed"
    r = c.get("/feed", follow_redirects=False)
    assert r.status_code == 200
    async def check():
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT terms_accepted_at, terms_version FROM users WHERE id = ?",
                (uid,))
            return await cur.fetchone()
    row = asyncio.run(check())
    assert row["terms_accepted_at"]
    assert row["terms_version"] == TERMS_VERSION


def test_open_redirect_blocked_on_accept():
    uid = _insert_user("terms_openredir")
    c = _login_client(uid)
    r = c.post("/terms/accept",
               data={"agree": "yes", "next": "https://evil.example/"},
               follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/feed"


# ---------------------------------------------------------------- staged Twitch OAuth flow
def test_oauth_new_user_is_staged_until_terms_accepted(monkeypatch):
    os.environ["TWITCH_CLIENT_ID"] = "test-id"
    os.environ["TWITCH_CLIENT_SECRET"] = "test-secret"
    try:
        async def fake_exchange(code, redirect_uri):
            return {"access_token": "tok"}
        async def fake_fetch(tok):
            return {"id": "999001", "login": "terms_twitchfan",
                    "display_name": "Terms Twitchfan",
                    "profile_image_url": "",
                    "description": "a fan"}
        monkeypatch.setattr(main_mod, "exchange_code", fake_exchange)
        monkeypatch.setattr(main_mod, "fetch_twitch_user", fake_fetch)

        c = _client()
        state = twitch_oauth.make_state()
        r = c.get("/auth/twitch/callback",
                  params={"code": "abc", "state": state},
                  follow_redirects=False)
        assert r.status_code == 303
        loc = r.headers["location"]
        assert loc.startswith("/terms?pending=")
        pending = dict(urllib.parse.parse_qsl(
            urllib.parse.urlsplit(loc).query))["pending"]
        assert read_pending_oauth_token(pending)["login"] == "terms_twitchfan"
        # account must NOT exist yet
        async def check_absent():
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT id FROM users WHERE twitch_id = ?", ("999001",))
                return await cur.fetchone()
        assert asyncio.run(check_absent()) is None
        # the interstitial explains the account is pending
        r = c.get(loc)
        assert r.status_code == 200
        assert "finish creating your BudzBook account" in r.text
        # accepting creates the account, stamps terms, logs them in
        r = c.post("/terms/accept",
                   data={"agree": "yes", "pending": pending},
                   follow_redirects=False)
        assert r.status_code == 303
        assert SESSION_COOKIE in r.headers.get("set-cookie", "")
        async def check_created():
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT username, terms_accepted_at, terms_version"
                    " FROM users WHERE twitch_id = ?", ("999001",))
                return await cur.fetchone()
        row = asyncio.run(check_created())
        assert row is not None
        assert row["terms_accepted_at"]
        assert row["terms_version"] == TERMS_VERSION
    finally:
        os.environ.pop("TWITCH_CLIENT_ID", None)
        os.environ.pop("TWITCH_CLIENT_SECRET", None)


def test_pending_oauth_token_roundtrip_and_tamper():
    tok = make_pending_oauth_token({"login": "x"})
    assert read_pending_oauth_token(tok)["login"] == "x"
    assert read_pending_oauth_token(tok + "tampered") is None
    assert read_pending_oauth_token("") is None
