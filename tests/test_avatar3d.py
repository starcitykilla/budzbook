"""Tests for 3D avatars (Ready Player Me) + the /crew showcase page.

Covers: the avatar_3d_url migration, the /settings/avatar3d save endpoint
(auth required, strict URL allowlist), and the public /crew page.
No network calls are made (no GLB is ever fetched in tests).
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="bb_avatar3d_test_")
os.environ["THC_SOCIAL_DB"] = os.path.join(_tmp, "test.db")
os.environ["THC_SOCIAL_MEDIA"] = os.path.join(_tmp, "media")
os.makedirs(os.path.join(_tmp, "media", "avatars"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "posts"), exist_ok=True)

import aiosqlite  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth import AGE_COOKIE, make_age_token  # noqa: E402
from app.db import DB_PATH, init_db  # noqa: E402
from app.main import app  # noqa: E402

asyncio.run(init_db())


def _client():
    c = TestClient(app)
    c.cookies.set(AGE_COOKIE, make_age_token())
    return c


def _register(c, username, password="secret123"):
    return c.post("/register", data={
        "username": username, "display_name": username.title(),
        "password": password, "twitch_username": username},
        follow_redirects=False)


def _login(c, username, password="secret123"):
    return c.post("/login", data={"username": username, "password": password},
                  follow_redirects=False)


async def _avatar3d_url(username):
    db = await aiosqlite.connect(DB_PATH)
    cur = await db.execute("SELECT avatar_3d_url FROM users WHERE username = ?",
                           (username,))
    row = await cur.fetchone()
    await db.close()
    return row[0] if row else None


GOOD_URL = "https://models.readyplayer.me/abc123.glb"
EVIL_URL = "https://evil.example.com/x.glb"


# ---------------------------------------------------------------- migration
def test_migration_adds_avatar3d_column():
    async def cols():
        db = await aiosqlite.connect(DB_PATH)
        cur = await db.execute("PRAGMA table_info(users)")
        names = {row[1] for row in await cur.fetchall()}
        await db.close()
        return names

    assert "avatar_3d_url" in asyncio.run(cols())


# ---------------------------------------------------------------- endpoint
def test_avatar3d_requires_login():
    c = _client()
    r = c.post("/settings/avatar3d", data={"avatar_url": GOOD_URL},
               follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_avatar3d_rejects_non_rpm_url():
    c = _client()
    _register(c, "alice3d")
    _login(c, "alice3d")
    r = c.post("/settings/avatar3d", data={"avatar_url": EVIL_URL},
               follow_redirects=False)
    assert r.status_code == 303
    assert "wasn" in r.headers["location"]  # "That avatar URL wasn't accepted"
    assert asyncio.run(_avatar3d_url("alice3d")) == ""


def test_avatar3d_rejects_empty_url():
    c = _client()
    _register(c, "bob3d")
    _login(c, "bob3d")
    r = c.post("/settings/avatar3d", data={"avatar_url": "   "},
               follow_redirects=False)
    assert r.status_code == 303
    assert asyncio.run(_avatar3d_url("bob3d")) == ""


def test_avatar3d_accepts_rpm_url():
    c = _client()
    _register(c, "carol3d")
    _login(c, "carol3d")
    r = c.post("/settings/avatar3d", data={"avatar_url": GOOD_URL},
               follow_redirects=False)
    assert r.status_code == 303
    assert "3D+avatar+saved" in r.headers["location"]
    assert asyncio.run(_avatar3d_url("carol3d")) == GOOD_URL


# ---------------------------------------------------------------- crew page
def test_crew_page_public_and_lists_3d_users():
    c = _client()  # never logs in: /crew must be public (age cookie only)
    r = c.get("/crew")
    assert r.status_code == 200
    assert "3D Crew" in r.text
    # carol3d saved a 3D avatar in the earlier test (same throwaway DB).
    assert "carol3d" in r.text
    assert GOOD_URL in r.text


def test_crew_page_empty_state():
    # Fresh DB with no 3D avatars at all.
    import shutil
    d2 = tempfile.mkdtemp(prefix="bb_avatar3d_empty_")
    os.environ["THC_SOCIAL_DB"] = os.path.join(d2, "empty.db")
    os.makedirs(os.path.join(d2, "media", "avatars"), exist_ok=True)
    os.makedirs(os.path.join(d2, "media", "posts"), exist_ok=True)
    from app import db as dbmod
    old_path, dbmod.DB_PATH = dbmod.DB_PATH, os.environ["THC_SOCIAL_DB"]
    try:
        asyncio.run(dbmod.init_db())
        c = _client()
        r = c.get("/crew")
        assert r.status_code == 200
        assert "No 3D avatars yet" in r.text
    finally:
        dbmod.DB_PATH = old_path
        shutil.rmtree(d2, ignore_errors=True)
