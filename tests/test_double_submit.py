"""Regression tests for double-submit protection.

A double-tapped Post/Comment/Send button must not create two rows. Covers:
posts, comments, DMs, likes (toggle), reports, the daily faucet claim, and
the register username race. The 30s server-side dedup window is shortened
via monkeypatched DEDUP_WINDOW_S where needed.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="bb_dupfix_test_")
os.environ["THC_SOCIAL_DB"] = os.path.join(_tmp, "test.db")
os.environ["THC_SOCIAL_MEDIA"] = os.path.join(_tmp, "media")
os.makedirs(os.path.join(_tmp, "media", "avatars"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "posts"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "comments"), exist_ok=True)

import aiosqlite  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import DB_PATH, init_db  # noqa: E402
import app.main as main  # noqa: E402
from app.main import app  # noqa: E402
from app.auth import make_age_token, AGE_COOKIE  # noqa: E402

asyncio.run(init_db())
client = TestClient(app, raise_server_exceptions=False)
client.cookies.set(AGE_COOKIE, make_age_token())


def _register(username, password="pw123456"):
    r = client.post("/register", data={"username": username, "password": password,
                                       "display_name": username})
    assert r.status_code in (200, 303), (username, r.status_code, r.text[:200])


def _login(username, password="pw123456"):
    r = client.post("/login", data={"username": username, "password": password})
    assert r.status_code in (200, 303), (username, r.status_code)


async def _count(table, where="", args=()):
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    cur = await db.execute(f"SELECT COUNT(*) AS c FROM {table} {where}", args)
    n = (await cur.fetchone())["c"]
    await db.close()
    return n


async def _user_id(username):
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    cur = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
    uid = (await cur.fetchone())["id"]
    await db.close()
    return uid


async def _balance(username, code="BUDZ"):
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT balance FROM wallets WHERE user_id = (SELECT id FROM users WHERE username = ?)"
        " AND currency_id = (SELECT id FROM currencies WHERE code = ?)",
        (username, code))
    row = await cur.fetchone()
    await db.close()
    return row["balance"] if row else 0


def _make_post(body):
    r = client.post("/post", data={"body": body})
    assert r.status_code in (200, 303), r.status_code
    return r


# ---- posts ----
def test_post_double_submit_creates_one():
    _register("dupper")
    _login("dupper")
    before = asyncio.run(_count("posts", "WHERE user_id = (SELECT id FROM users WHERE username = 'dupper')"))
    _make_post("double tap test post")
    _make_post("double tap test post")  # the accidental second tap
    after = asyncio.run(_count("posts", "WHERE user_id = (SELECT id FROM users WHERE username = 'dupper')"))
    assert after - before == 1


def test_post_different_bodies_both_saved():
    _login("dupper")
    before = asyncio.run(_count("posts", "WHERE user_id = (SELECT id FROM users WHERE username = 'dupper')"))
    _make_post("rapid post one xyz")
    _make_post("rapid post two xyz")
    after = asyncio.run(_count("posts", "WHERE user_id = (SELECT id FROM users WHERE username = 'dupper')"))
    assert after - before == 2


def test_post_identical_body_after_window_saves_again():
    _login("dupper")
    old = main.DEDUP_WINDOW_S
    main.DEDUP_WINDOW_S = -1  # expire the window instantly
    try:
        before = asyncio.run(_count("posts", "WHERE user_id = (SELECT id FROM users WHERE username = 'dupper')"))
        _make_post("repost after window abc")
        _make_post("repost after window abc")
        after = asyncio.run(_count("posts", "WHERE user_id = (SELECT id FROM users WHERE username = 'dupper')"))
        assert after - before == 2
    finally:
        main.DEDUP_WINDOW_S = old


# ---- comments ----
def test_comment_double_submit_creates_one():
    _login("dupper")
    _make_post("post for comment dedup test")
    import re
    r = client.get("/feed")
    m = re.search(r"/post/(\d+)/comment", r.text)
    assert m, "comment form not found"
    pid = m.group(1)
    n0 = asyncio.run(_count("comments", "WHERE post_id = ?", (pid,)))
    for _ in range(2):
        r = client.post(f"/post/{pid}/comment", data={"body": "double tap comment"})
        assert r.status_code in (200, 303), r.status_code
    n1 = asyncio.run(_count("comments", "WHERE post_id = ?", (pid,)))
    assert n1 - n0 == 1


# ---- DMs ----
def test_dm_double_submit_creates_one():
    _register("dm_sender")
    _register("dm_peer")
    _login("dm_sender")
    n0 = asyncio.run(_count("messages"))
    for _ in range(2):
        r = client.post("/messages/dm_peer/send", data={"body": "double tap dm"})
        assert r.status_code in (200, 303), r.status_code
    n1 = asyncio.run(_count("messages"))
    assert n1 - n0 == 1


# ---- likes: toggle semantics, no duplicate rows ----
def test_like_double_click_no_duplicate_rows():
    _login("dupper")
    _make_post("post for like dedup test")
    import re
    r = client.get("/feed")
    m = re.search(r"/post/(\d+)/like", r.text)
    assert m
    pid = m.group(1)
    uid = asyncio.run(_user_id("dupper"))
    for _ in range(2):  # tap, tap -> like then unlike
        r = client.post(f"/post/{pid}/like")
        assert r.status_code in (200, 303)
    n = asyncio.run(_count("likes", "WHERE user_id = ? AND post_id = ?", (uid, int(pid))))
    assert n == 0  # toggled twice: back to unliked, never duplicated
    r = client.post(f"/post/{pid}/like")  # single tap -> liked
    assert r.status_code in (200, 303)
    n = asyncio.run(_count("likes", "WHERE user_id = ? AND post_id = ?", (uid, int(pid))))
    assert n == 1


# ---- reports ----
def test_report_double_submit_creates_one():
    _register("reporter")
    _register("reportee")
    _login("reportee")
    _make_post("post to be reported")
    import re
    r = client.get("/feed")
    m = re.search(r"/post/(\d+)/report", r.text)
    assert m
    pid = m.group(1)
    _login("reporter")
    n0 = asyncio.run(_count("reports", "WHERE post_id = ?", (pid,)))
    for _ in range(2):
        r = client.post(f"/post/{pid}/report", data={"reason": "spam"})
        assert r.status_code in (200, 303)
    n1 = asyncio.run(_count("reports", "WHERE post_id = ?", (pid,)))
    assert n1 - n0 == 1


# ---- daily faucet: double-tap awards once ----
def test_wallet_claim_double_tap_awards_once():
    from app.currency import DAILY_CLAIM_AMOUNT
    _register("claimer")
    _login("claimer")
    b0 = asyncio.run(_balance("claimer"))
    for _ in range(2):
        r = client.post("/wallet/claim")
        assert r.status_code in (200, 303), r.status_code
    b1 = asyncio.run(_balance("claimer"))
    assert b1 - b0 == DAILY_CLAIM_AMOUNT


# ---- register: duplicate username handled, no 500 ----
def test_register_duplicate_username_no_crash():
    _register("unique_name_1")
    r = client.post("/register", data={"username": "unique_name_1",
                                       "password": "pw123456",
                                       "display_name": "unique_name_1"})
    assert r.status_code in (200, 400, 303), r.status_code
    assert "taken" in r.text.lower() or r.status_code == 303
