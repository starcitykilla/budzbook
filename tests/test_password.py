"""Tests for the self-service password change form (/settings/password)."""
import asyncio
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="bb_pw_test_")
os.environ["THC_SOCIAL_DB"] = os.path.join(_tmp, "test.db")
os.environ["THC_SOCIAL_MEDIA"] = os.path.join(_tmp, "media")
os.makedirs(os.path.join(_tmp, "media", "avatars"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "posts"), exist_ok=True)

import aiosqlite  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth import make_age_token, AGE_COOKIE, verify_password  # noqa: E402
from app.db import DB_PATH, init_db  # noqa: E402
from app.main import app  # noqa: E402

asyncio.run(init_db())
client = TestClient(app, raise_server_exceptions=False)
client.cookies.set(AGE_COOKIE, make_age_token())


async def _hash_of(username):
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    cur = await db.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
    h = (await cur.fetchone())["password_hash"]
    await db.close()
    return h


def _register(username, password):
    r = client.post("/register", data={"username": username, "password": password,
                                       "display_name": username,
        "agree_terms": "yes"},
                    follow_redirects=False)
    assert r.status_code in (200, 303), (username, r.status_code)


def _login(username, password):
    r = client.post("/login", data={"username": username, "password": password},
                    follow_redirects=False)
    assert r.status_code in (200, 303), (username, r.status_code)


def _change(current, new, confirm):
    return client.post("/settings/password",
                       data={"current_password": current, "new_password": new,
                             "confirm_password": confirm},
                       follow_redirects=False)


def test_change_password_happy_path():
    _register("pwuser1", "oldpass1")
    _login("pwuser1", "oldpass1")
    r = _change("oldpass1", "newpass2", "newpass2")
    assert r.status_code == 303, r.status_code
    assert "Password+changed" in r.headers["location"]
    assert verify_password("newpass2", asyncio.run(_hash_of("pwuser1")))
    assert not verify_password("oldpass1", asyncio.run(_hash_of("pwuser1")))
    # new password logs in, old one does not
    client.cookies.clear()
    client.cookies.set(AGE_COOKIE, make_age_token())
    r = client.post("/login", data={"username": "pwuser1", "password": "newpass2"},
                    follow_redirects=False)
    assert r.status_code == 303, r.status_code
    r = client.post("/login", data={"username": "pwuser1", "password": "oldpass1"},
                    follow_redirects=False)
    assert r.status_code == 400, r.status_code


def test_change_password_wrong_current():
    _register("pwuser2", "oldpass1")
    _login("pwuser2", "oldpass1")
    before = asyncio.run(_hash_of("pwuser2"))
    r = _change("nottherightone", "newpass2", "newpass2")
    assert r.status_code == 303, r.status_code
    assert "Current+password+is+wrong" in r.headers["location"]
    assert asyncio.run(_hash_of("pwuser2")) == before


def test_change_password_mismatch_confirm():
    _register("pwuser3", "oldpass1")
    _login("pwuser3", "oldpass1")
    before = asyncio.run(_hash_of("pwuser3"))
    r = _change("oldpass1", "newpass2", "different3")
    assert r.status_code == 303, r.status_code
    assert "do+not+match" in r.headers["location"]
    assert asyncio.run(_hash_of("pwuser3")) == before


def test_change_password_too_short():
    _register("pwuser4", "oldpass1")
    _login("pwuser4", "oldpass1")
    before = asyncio.run(_hash_of("pwuser4"))
    r = _change("oldpass1", "abc", "abc")
    assert r.status_code == 303, r.status_code
    assert "at+least+6" in r.headers["location"]
    assert asyncio.run(_hash_of("pwuser4")) == before


def test_change_password_requires_login():
    anon = TestClient(app, raise_server_exceptions=False)
    anon.cookies.set(AGE_COOKIE, make_age_token())
    r = anon.post("/settings/password",
                  data={"current_password": "x", "new_password": "newpass2",
                        "confirm_password": "newpass2"},
                  follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert "/login" in r.headers["location"]
