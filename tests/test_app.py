"""Pytest suite for The Higher Club v1.

Uses an isolated throwaway SQLite DB + media dir (via env vars read by
app.db at import time), so the real dev database is never touched.
"""
import asyncio
import io
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="thc_test_")
os.environ["THC_SOCIAL_DB"] = os.path.join(_tmp, "test.db")
os.environ["THC_SOCIAL_MEDIA"] = os.path.join(_tmp, "media")
# StaticFiles requires the directory to exist at app import time.
os.makedirs(os.path.join(_tmp, "media", "avatars"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "posts"), exist_ok=True)

# Hermetic casino DB for leaderboard tests (the adapter reads CASINO_DB at
# call time; production default is the bot's live cannabet.db).
_casino_db = os.path.join(_tmp, "casino.db")
os.environ["CASINO_DB"] = _casino_db


def _make_casino_db(rows):
    import sqlite3
    if os.path.exists(_casino_db):
        os.remove(_casino_db)
    c = sqlite3.connect(_casino_db)
    c.execute("CREATE TABLE users (username TEXT PRIMARY KEY,"
              " balance INTEGER NOT NULL DEFAULT 0)")
    c.executemany("INSERT INTO users (username, balance) VALUES (?, ?)", rows)
    c.commit()
    c.close()

import aiosqlite  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.auth import hash_password, AGE_COOKIE, make_age_token  # noqa: E402
from app.db import DB_PATH, init_db, avatar_path_for  # noqa: E402
from app.main import app  # noqa: E402

asyncio.run(init_db())
client = TestClient(app)
# The 21+ age gate fronts every page; tests run as a verified adult.
client.cookies.set(AGE_COOKIE, make_age_token())


def _png_bytes(color=(60, 120, 60)):
    img = Image.new("RGB", (100, 100), color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _register(username, password="secret123"):
    return client.post("/register", data={
        "username": username, "display_name": username.title(),
        "password": password, "twitch_username": username},
        follow_redirects=False)


def _login(username, password="secret123"):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)


async def _make_admin():
    db = await aiosqlite.connect(DB_PATH)
    await db.execute(
        "INSERT INTO users (username, display_name, password_hash, is_admin, created_at)"
        " VALUES ('mod', 'Mod', ?, 1, 'now')", (hash_password("modpass"),))
    await db.commit()
    await db.close()


async def _approve(username):
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("UPDATE users SET avatar_approved = 1 WHERE username = ?", (username,))
    await db.commit()
    await db.close()


# ---------------------------------------------------------------- auth
def test_register_and_login_logout():
    r = _register("alice")
    assert r.status_code == 303 and r.headers["location"].startswith("/feed")
    # logged in: feed loads
    assert client.get("/feed").status_code == 200
    # logout
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    # feed now redirects to login
    r = client.get("/feed", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    # login again works
    r = _login("alice")
    assert r.status_code == 303
    assert client.get("/feed").status_code == 200


def test_register_bad_username_and_duplicate():
    r = client.post("/register", data={"username": "ab", "display_name": "x",
                                       "password": "secret123", "twitch_username": ""})
    assert r.status_code == 400  # too short
    r = client.post("/login", data={"username": "alice", "password": "wrongpass"})
    assert r.status_code == 400  # bad password


# ---------------------------------------------------------------- feed/posts
def test_post_like_comment_delete():
    _login("alice")
    r = client.post("/post", data={"body": "hello club", "kind": "post"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/feed")
    assert "hello club" in page.text
    # find post id via like toggle round-trip: like then unlike
    import re
    m = re.search(r'/post/(\d+)/like', page.text)
    pid = m.group(1)
    client.post(f"/post/{pid}/like", follow_redirects=False)
    page = client.get("/feed")
    assert "💚 1" in page.text
    client.post(f"/post/{pid}/like", follow_redirects=False)
    assert "🤍 0" in client.get("/feed").text
    # comment
    client.post(f"/post/{pid}/comment", data={"body": "nice post"},
                follow_redirects=False)
    assert "nice post" in client.get("/feed").text
    # delete own post
    client.post(f"/post/{pid}/delete", follow_redirects=False)
    assert "hello club" not in client.get("/feed").text


def test_post_too_long_rejected():
    _login("alice")
    r = client.post("/post", data={"body": "x" * 1000, "kind": "post"},
                    follow_redirects=False)
    assert r.status_code == 303  # exactly at the 1000-char cap: accepted
    r = client.post("/post", data={"body": "x" * 1001, "kind": "post"},
                    follow_redirects=False)
    assert r.status_code == 303 and "1000" in r.headers["location"]


# ---------------------------------------------------------------- social graph
def test_follow_unfollow_and_counts():
    _register("bob")
    _login("bob")
    client.post("/u/alice/follow", follow_redirects=False)
    page = client.get("/u/alice")
    assert ">1</b> followers" in page.text
    assert "Unfollow" in page.text
    assert "/u/alice/followers" in page.text
    assert "bob" in client.get("/u/alice/followers").text
    # following filter shows alice's posts to bob
    client.post("/u/alice/unfollow", follow_redirects=False)
    assert ">0</b> followers" in client.get("/u/alice").text


# ---------------------------------------------------------------- DMs
def test_dm_send_and_poll():
    _login("alice")
    client.post("/messages/bob/send", data={"body": "hey bob"},
                follow_redirects=False)
    page = client.get("/messages/bob")
    assert "hey bob" in page.text
    data = client.get("/messages/bob/poll?after_id=0").json()
    assert any(m["body"] == "hey bob" and m["mine"] for m in data["messages"])
    data = client.get("/messages/bob/poll?after_id=999999").json()
    assert data["messages"] == []


# ---------------------------------------------------------------- moderation
def test_report_and_admin_delete():
    asyncio.run(_make_admin())
    _login("alice")
    client.post("/post", data={"body": "spammy post", "kind": "post"},
                follow_redirects=False)
    import re
    m = re.search(r'/post/(\d+)/like', client.get("/feed").text)
    pid = m.group(1)
    client.post("/logout", follow_redirects=False)
    _login("bob")
    r = client.post(f"/post/{pid}/report", data={"reason": "spam"},
                    follow_redirects=False)
    assert r.status_code == 303
    # bob (not admin) can't see admin page
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303
    # admin sees the report and can delete the post
    client.post("/logout", follow_redirects=False)
    _login("mod", "modpass")
    assert "spammy post" in client.get("/admin").text
    cur_pid = pid
    import re as _re
    m = _re.search(r'/admin/reports/(\d+)/resolve', client.get("/admin").text)
    rid = m.group(1)
    client.post(f"/admin/reports/{rid}/resolve", data={"action": "delete"},
                follow_redirects=False)
    assert "spammy post" not in client.get("/feed").text


# ---------------------------------------------------------------- avatars + stream API
def test_avatar_upload_approval_and_stream_api():
    _login("alice")
    png = _png_bytes()
    r = client.post("/settings",
                    data={"display_name": "Alice", "bio": "", "twitch_username": "alice"},
                    files={"avatar": ("face.png", png, "image/png")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert os.path.isfile(avatar_path_for("alice"))
    # not approved yet -> hidden from stream api
    assert "alice" not in client.get("/api/stream/avatars").json()
    assert client.get("/api/stream/avatar/alice").status_code == 404
    # admin approves
    client.post("/logout", follow_redirects=False)
    _login("mod", "modpass")
    client.post("/admin/avatar/alice/approve", follow_redirects=False)
    data = client.get("/api/stream/avatars").json()
    assert "alice" in data and data["alice"].endswith("/media/avatars/alice.jpg")
    # twitch-keyed variant
    data = client.get("/api/stream/avatars?key=twitch").json()
    assert "alice" in data  # twitch_username == alice
    r = client.get("/api/stream/avatar/alice")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    # reject flow removes the file
    _login("alice")
    client.post("/settings",
                data={"display_name": "Alice", "bio": "", "twitch_username": "alice"},
                files={"avatar": ("face2.png", png, "image/png")},
                follow_redirects=False)
    client.post("/logout", follow_redirects=False)
    _login("mod", "modpass")
    client.post("/admin/avatar/alice/reject", follow_redirects=False)
    assert not os.path.isfile(avatar_path_for("alice"))
    assert "alice" not in client.get("/api/stream/avatars").json()


def test_avatar_rejects_non_image():
    _login("alice")
    r = client.post("/settings",
                    data={"display_name": "Alice", "bio": "", "twitch_username": ""},
                    files={"avatar": ("evil.txt", b"not an image", "text/plain")},
                    follow_redirects=False)
    assert r.status_code == 303 and "/settings?msg=" in r.headers["location"]


# ---------------------------------------------------------------- THC tie-ins
def test_tiein_pages():
    _login("alice")
    for path in ("/leaderboard", "/sportsbook", "/grow"):
        r = client.get(path)
        assert r.status_code == 200, path
    _make_casino_db([("stripemike", 2000), ("goldleaf", 750)])
    r = client.get("/leaderboard")
    assert "stripemike" in r.text and "2000" in r.text
    assert "goldleaf" in r.text
    assert "Demo data" not in r.text


def test_watch_grid_page():
    client.post("/logout", follow_redirects=False)
    # anonymous -> redirected to login
    r = client.get("/watch", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    _register("watchfan")
    _login("watchfan")
    r = client.get("/watch")
    assert r.status_code == 200, "/watch"
    # followed-streams grid: card links to the per-channel player view
    assert "Followed Streams" in r.text
    assert "krzy_budz" in r.text
    assert "/watch/krzy_budz" in r.text
    # no Twitch creds in tests -> fail-soft offline cards
    assert "OFFLINE" in r.text


def test_watch_channel_player_embeds():
    client.post("/logout", follow_redirects=False)
    # anonymous -> redirected to login
    r = client.get("/watch/krzy_budz", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    _login("watchfan")
    r = client.get("/watch/krzy_budz")
    assert r.status_code == 200, "/watch/krzy_budz"
    assert "player.twitch.tv" in r.text
    assert "twitch.tv/embed/krzy_budz/chat" in r.text
    assert "parent=budzbook.us" in r.text
    assert "/watch" in r.text  # back link to the grid
    # channel names are case-insensitive
    r = client.get("/watch/KRZY_BUDZ")
    assert r.status_code == 200
    # unknown channel -> 404 page
    r = client.get("/watch/not_a_real_channel_xyz")
    assert r.status_code == 404


def test_leaderboard_falls_back_to_demo():
    _login("alice")
    old = os.environ.get("CASINO_DB")
    os.environ["CASINO_DB"] = os.path.join(_tmp, "nope.db")
    try:
        r = client.get("/leaderboard")
        assert r.status_code == 200
        assert "Demo data" in r.text
        assert "krzy_budz" in r.text
    finally:
        if old is None:
            del os.environ["CASINO_DB"]
        else:
            os.environ["CASINO_DB"] = old


def test_welcome_bonus_on_register():
    from app.currency import grant_welcome_bonus, WELCOME_BONUS_BUDZ
    _register("newbie")
    assert WELCOME_BONUS_BUDZ == 100

    async def _bal_and_grant():
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id FROM users WHERE username = 'newbie'")
        uid = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT balance FROM wallets WHERE user_id = ?"
            " AND currency_id = (SELECT id FROM currencies WHERE code = 'BUDZ')",
            (uid,))
        bal = (await cur.fetchone())[0]
        again = await grant_welcome_bonus(db, uid)  # idempotent: no double-grant
        cur = await db.execute(
            "SELECT balance FROM wallets WHERE user_id = ?"
            " AND currency_id = (SELECT id FROM currencies WHERE code = 'BUDZ')",
            (uid,))
        bal2 = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM currency_txns WHERE user_id = ?"
            " AND reason = 'welcome bonus'", (uid,))
        n = (await cur.fetchone())[0]
        await db.close()
        return bal, again, bal2, n

    bal, again, bal2, n = asyncio.run(_bal_and_grant())
    assert bal == 100
    assert again is False and bal2 == 100 and n == 1
