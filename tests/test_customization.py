"""Tests for GIF comments (Tenor), profile customization, and feed customization."""
import asyncio
import io
import json
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="bb_custom_test_")
os.environ["THC_SOCIAL_DB"] = os.path.join(_tmp, "test.db")
os.environ["THC_SOCIAL_MEDIA"] = os.path.join(_tmp, "media")
os.makedirs(os.path.join(_tmp, "media", "avatars"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "posts"), exist_ok=True)

import aiosqlite  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.auth import make_age_token, AGE_COOKIE  # noqa: E402
from app.db import DB_PATH, MEDIA_DIR, init_db  # noqa: E402
from app.main import app  # noqa: E402

asyncio.run(init_db())
client = TestClient(app, raise_server_exceptions=False)
client.cookies.set(AGE_COOKIE, make_age_token())


def _register(username, password="pw123456"):
    r = client.post("/register", data={"username": username, "password": password,
                                       "display_name": username},
                    follow_redirects=False)
    assert r.status_code in (200, 303), (username, r.status_code)


def _login(username, password="pw123456"):
    r = client.post("/login", data={"username": username, "password": password},
                    follow_redirects=False)
    assert r.status_code in (200, 303), (username, r.status_code)


def _png_bytes(color=(60, 120, 60), size=(100, 100)):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def _db_one(sql, args=()):
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    cur = await db.execute(sql, args)
    row = await cur.fetchone()
    await db.close()
    return row


async def _db_cols(table):
    db = await aiosqlite.connect(DB_PATH)
    cur = await db.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in await cur.fetchall()]
    await db.close()
    return cols


def _post_id():
    return asyncio.run(_db_one("SELECT id FROM posts ORDER BY id DESC LIMIT 1"))["id"]


# ---- migrations ----
def test_migration_columns_exist():
    assert "gif_url" in asyncio.run(_db_cols("comments"))
    for col in ("theme_color", "banner_path", "feed_prefs"):
        assert col in asyncio.run(_db_cols("users")), col


# ---- GIF search route ----
def test_gif_search_503_without_key(monkeypatch):
    monkeypatch.delenv("TENOR_API_KEY", raising=False)
    _register("gifgal")
    _login("gifgal")
    r = client.get("/api/gif/search?q=cats")
    assert r.status_code == 503


def test_gif_search_proxied_with_key(monkeypatch):
    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{
                "media_formats": {
                    "gif": {"url": "https://media.tenor.com/x.gif"},
                    "tinygif": {"url": "https://media.tenor.com/x_tiny.gif"},
                }}]}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            assert "tenor.googleapis.com" in url
            assert params["key"] == "fake-key"
            return _FakeResp()

    monkeypatch.setenv("TENOR_API_KEY", "fake-key")
    monkeypatch.setattr("app.main.httpx.AsyncClient", _FakeClient)
    _login("gifgal")
    r = client.get("/api/gif/search?q=cats")
    assert r.status_code == 200
    body = r.json()
    assert body["results"][0]["url"] == "https://media.tenor.com/x.gif"
    assert body["results"][0]["preview"] == "https://media.tenor.com/x_tiny.gif"


# ---- GIF comments ----
def test_comment_with_gif_renders():
    _login("gifgal")
    client.post("/post", data={"body": "a post for gif comments", "kind": "post"})
    pid = _post_id()
    r = client.post(f"/post/{pid}/comment",
                    data={"body": "lol", "gif_url": "https://media.tenor.com/x.gif"},
                    follow_redirects=False)
    assert r.status_code == 303
    row = asyncio.run(_db_one(
        "SELECT gif_url FROM comments WHERE post_id = ? ORDER BY id DESC LIMIT 1", (pid,)))
    assert row["gif_url"] == "https://media.tenor.com/x.gif"
    feed = client.get("/feed")
    assert 'class="comment-gif"' in feed.text
    assert "https://media.tenor.com/x.gif" in feed.text
    assert "gif-picker-btn" in feed.text


def test_gif_only_comment_allowed():
    _login("gifgal")
    pid = _post_id()
    before = asyncio.run(_db_one(
        "SELECT COUNT(*) AS n FROM comments WHERE post_id = ?", (pid,)))["n"]
    r = client.post(f"/post/{pid}/comment",
                    data={"body": "", "gif_url": "https://media.tenor.com/y.gif"},
                    follow_redirects=False)
    assert r.status_code == 303
    after = asyncio.run(_db_one(
        "SELECT COUNT(*) AS n FROM comments WHERE post_id = ?", (pid,)))["n"]
    assert after == before + 1


def test_bad_gif_url_dropped():
    _login("gifgal")
    pid = _post_id()
    client.post(f"/post/{pid}/comment",
                data={"body": "x", "gif_url": "javascript:alert(1)"},
                follow_redirects=False)
    row = asyncio.run(_db_one(
        "SELECT gif_url FROM comments WHERE post_id = ? ORDER BY id DESC LIMIT 1", (pid,)))
    assert row["gif_url"] == ""


# ---- profile customization ----
def test_theme_color_applies_and_invalid_rejected():
    _register("themefan")
    _login("themefan")
    client.post("/settings", data={"display_name": "themefan", "bio": "",
                                  "theme_color": "#a06cd5"})
    row = asyncio.run(_db_one("SELECT theme_color FROM users WHERE username='themefan'"))
    assert row["theme_color"] == "#a06cd5"
    page = client.get("/u/themefan")
    assert "#a06cd5" in page.text
    assert "settings#profile-look" in page.text  # gear link on own profile
    # invalid color is ignored
    client.post("/settings", data={"display_name": "themefan", "bio": "",
                                  "theme_color": "not-a-color"})
    row = asyncio.run(_db_one("SELECT theme_color FROM users WHERE username='themefan'"))
    assert row["theme_color"] == ""


def test_banner_upload():
    _login("themefan")
    png = _png_bytes(size=(1600, 400))
    r = client.post("/settings", data={"display_name": "themefan", "bio": ""},
                    files={"banner": ("banner.png", png, "image/png")},
                    follow_redirects=False)
    assert r.status_code == 303
    row = asyncio.run(_db_one("SELECT banner_path FROM users WHERE username='themefan'"))
    assert row["banner_path"] == "banners/themefan.jpg"
    assert os.path.exists(os.path.join(MEDIA_DIR, "banners", "themefan.jpg"))
    page = client.get("/u/themefan")
    assert "profile-banner" in page.text


def test_settings_page_has_new_sections():
    _login("themefan")
    page = client.get("/settings")
    assert 'id="profile-look"' in page.text
    assert 'id="feed"' in page.text
    assert 'name="theme_color"' in page.text
    assert 'name="feed_sort"' in page.text
    assert 'name="feed_muted_words"' in page.text


# ---- feed customization ----
def test_feed_muted_words_and_gear():
    _register("feedfan")
    _login("feedfan")
    client.post("/post", data={"body": "i love spoilers tonight", "kind": "post"})
    client.post("/post", data={"body": "totally clean post here", "kind": "post"})
    client.post("/settings", data={"display_name": "feedfan", "bio": "",
                                  "feed_muted_words": "spoilers"})
    row = asyncio.run(_db_one("SELECT feed_prefs FROM users WHERE username='feedfan'"))
    prefs = json.loads(row["feed_prefs"])
    assert prefs["muted_words"] == "spoilers"
    feed = client.get("/feed")
    assert "totally clean post here" in feed.text
    assert "i love spoilers tonight" not in feed.text
    assert 'href="/settings#feed"' in feed.text  # gear icon on feed


def test_feed_top_sort():
    _register("liker1")
    _register("liker2")
    _login("feedfan")
    client.post("/post", data={"body": "postAAA zero likes", "kind": "post"})
    client.post("/post", data={"body": "postBBB two likes", "kind": "post"})
    pid_b = _post_id()
    _login("liker1")
    client.post(f"/post/{pid_b}/like")
    _login("liker2")
    client.post(f"/post/{pid_b}/like")
    _login("feedfan")
    client.post("/settings", data={"display_name": "feedfan", "bio": "",
                                  "feed_sort": "top"})
    feed = client.get("/feed").text
    assert feed.index("postBBB two likes") < feed.index("postAAA zero likes")
