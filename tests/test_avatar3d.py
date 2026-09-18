"""Tests for the generic 3D GLB viewers + the /crew showcase page.

The Avaturn editor was removed from the site, and the in-house creature
builder was ripped out afterwards. What remains: the avatar_3d_url
column/migration, the generic Three.js GLB viewers (profile, /crew) that
render already-saved URLs, and the public /crew page. Asserts /settings no
longer references Avaturn or the builder.
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
        "password": password, "twitch_username": username,
        "agree_terms": "yes"},
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


# ------------------------------------------------------- settings page render
def test_settings_page_has_no_avaturn_editor():
    # Avaturn was ripped out: the editor, its SDK, and the manual .glb
    # link form are gone. The creature builder was ripped out too: only
    # the 2D avatar upload section remains on /settings.
    c = _client()
    _register(c, "dave3d")
    _login(c, "dave3d")
    r = c.get("/settings")
    assert r.status_code == 200
    low = r.text.lower()
    assert "avaturn" not in low
    assert "avaturn-sdk-container" not in r.text
    assert "avatar3d" not in r.text  # manual .glb link form removed
    assert "Build your creature" not in r.text
    assert "builder-wrap" not in r.text
    assert "avatar_builder" not in r.text
    assert "Avatar (square JPG/PNG" in r.text  # 2D upload still there


# ---------------------------------------------------------------- crew page
def test_crew_page_public_and_lists_3d_users():
    c = _client()  # never logs in: /crew must be public (age cookie only)
    r = c.get("/crew")
    assert r.status_code == 200
    assert "3D Crew" in r.text
    # Seed a 3D avatar directly: the generic GLB viewer path must still work
    # for already-saved avatar_3d_url values.
    _register(c, "carol3d")
    async def seed():
        db = await aiosqlite.connect(DB_PATH)
        await db.execute(
            "UPDATE users SET avatar_3d_url = ? WHERE username = ?",
            ("https://assets.avaturn.me/abc123.glb", "carol3d"))
        await db.commit()
        await db.close()
    asyncio.run(seed())
    r = c.get("/crew")
    assert r.status_code == 200
    assert "carol3d" in r.text
    assert "https://assets.avaturn.me/abc123.glb" in r.text


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

