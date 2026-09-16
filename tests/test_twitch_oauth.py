"""Tests for Twitch OAuth login (BudzBook).

Covers: state signing/tamper-resistance, authorize URL shape, the
unconfigured-credentials path, callback error paths, and the DB migration
that adds the Twitch link columns. No network calls are made.
"""
import asyncio
import os
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="bb_twitch_test_")
os.environ["THC_SOCIAL_DB"] = os.path.join(_tmp, "test.db")
os.makedirs(os.path.join(_tmp, "media", "avatars"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "posts"), exist_ok=True)
# Make sure the OAuth credentials are NOT configured in the test env.
os.environ.pop("TWITCH_CLIENT_ID", None)
os.environ.pop("TWITCH_CLIENT_SECRET", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import twitch_oauth  # noqa: E402
from app.auth import AGE_COOKIE, make_age_token  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

asyncio.run(init_db())
client = TestClient(app)
client.cookies.set(AGE_COOKIE, make_age_token())


# ---------------------------------------------------------------- state
def test_state_roundtrip():
    s = twitch_oauth.make_state(link_uid=42)
    data = twitch_oauth.read_state(s)
    assert data is not None
    assert data["link_uid"] == 42
    assert data["nonce"]


def test_state_tampered_rejected():
    s = twitch_oauth.make_state()
    assert twitch_oauth.read_state(s + "x") is None
    assert twitch_oauth.read_state("") is None
    assert twitch_oauth.read_state("not-a-token") is None


# ---------------------------------------------------------------- URL
def test_authorize_url_shape():
    url = twitch_oauth.authorize_url(
        "http://localhost:8000/auth/twitch/callback", "state123")
    assert url.startswith("https://id.twitch.tv/oauth2/authorize?")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["response_type"] == "code"
    assert q["redirect_uri"] == "http://localhost:8000/auth/twitch/callback"
    assert q["state"] == "state123"
    assert "user:read:email" in q["scope"]


def test_not_configured_without_credentials():
    assert twitch_oauth.configured() is False


# ---------------------------------------------------------------- routes
def test_twitch_start_redirects_to_login_when_unconfigured():
    r = client.get("/auth/twitch", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?msg=Twitch+login+isn")


def test_twitch_callback_rejects_bad_state():
    r = client.get("/auth/twitch/callback",
                   params={"code": "abc", "state": "bogus"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert "/login?msg=" in r.headers["location"]


def test_twitch_callback_handles_provider_error():
    r = client.get("/auth/twitch/callback",
                   params={"error": "access_denied"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert "cancelled" in r.headers["location"]


def test_login_page_hides_twitch_button_when_unconfigured():
    r = client.get("/login")
    assert r.status_code == 200
    assert "Continue with Twitch" not in r.text
    r = client.get("/register")
    assert r.status_code == 200
    assert "Continue with Twitch" not in r.text


# ---------------------------------------------------------------- migration
def test_migration_adds_twitch_columns():
    import aiosqlite
    from app.db import DB_PATH

    async def cols():
        db = await aiosqlite.connect(DB_PATH)
        cur = await db.execute("PRAGMA table_info(users)")
        names = {row[1] for row in await cur.fetchall()}
        await db.close()
        return names

    assert {"twitch_id", "twitch_avatar", "twitch_verified"} <= asyncio.run(cols())
