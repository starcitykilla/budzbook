"""Tests for BudzBook emoji reactions (posts + comments)."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="bb_reactions_test_")
os.environ["THC_SOCIAL_DB"] = os.path.join(_tmp, "test.db")
os.environ["THC_SOCIAL_MEDIA"] = os.path.join(_tmp, "media")
os.makedirs(os.path.join(_tmp, "media", "avatars"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "posts"), exist_ok=True)

import aiosqlite  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import DB_PATH, init_db, _migrate_social  # noqa: E402
from app.main import app  # noqa: E402
from app.auth import make_age_token, AGE_COOKIE  # noqa: E402

asyncio.run(init_db())
client = TestClient(app, raise_server_exceptions=False)
client.cookies.set(AGE_COOKIE, make_age_token())
JSON = {"Accept": "application/json"}


def _register(username, password="pw123456"):
    r = client.post("/register", data={"username": username, "password": password,
                                       "display_name": username})
    assert r.status_code in (200, 303), (username, r.status_code)


def _login(username, password="pw123456"):
    r = client.post("/login", data={"username": username, "password": password})
    assert r.status_code in (200, 303), (username, r.status_code)


async def _db_one(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, args)
        return await cur.fetchone()


async def _db_all(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, args)
        return await cur.fetchall()


def _post_id(body):
    return asyncio.run(_db_one("SELECT id FROM posts WHERE body = ?", (body,)))[0]


def _comment_id(body):
    return asyncio.run(_db_one("SELECT id FROM comments WHERE body = ?", (body,)))[0]


def _mk_post(user, body):
    _register(user)
    _login(user)
    r = client.post("/post", data={"body": body})
    assert r.status_code in (200, 303), (user, r.status_code)
    return _post_id(body)


def test_legacy_likes_migrate_to_reactions():
    """Old likes table folds into reactions, then is dropped."""
    path = os.path.join(_tmp, "mig.db")

    async def go():
        async with aiosqlite.connect(path) as db:
            await db.execute("CREATE TABLE likes (user_id INTEGER NOT NULL,"
                             " post_id INTEGER NOT NULL, created_at TEXT NOT NULL,"
                             " PRIMARY KEY (user_id, post_id))")
            await db.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                             " user_id INTEGER NOT NULL, body TEXT NOT NULL,"
                             " created_at TEXT NOT NULL)")
            await db.execute("CREATE TABLE comments (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                             " post_id INTEGER NOT NULL, user_id INTEGER NOT NULL,"
                             " body TEXT NOT NULL, created_at TEXT NOT NULL)")
            await db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT)")
            await db.execute("INSERT INTO likes VALUES (1, 7, '2026-09-17T00:00:00')")
            await db.execute("INSERT INTO likes VALUES (2, 7, '2026-09-17T00:00:00')")
            await db.commit()
            await _migrate_social(db)
            await db.commit()
            cur = await db.execute(
                "SELECT user_id, target, target_id, emoji FROM reactions ORDER BY user_id")
            rows = [(r[0], r[1], r[2], r[3]) for r in await cur.fetchall()]
            assert rows == [(1, "post", 7, "like"), (2, "post", 7, "like")]
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='likes'")
            assert await cur.fetchone() is None

    asyncio.run(go())


def test_react_post_toggle_on_off():
    pid = _mk_post("rxn_alice", "rxn toggle post")
    r = client.post("/react", data={"target": "post", "target_id": pid,
                                    "emoji": "love"}, headers=JSON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] and d["active"] and d["my_reaction"] == "love"
    assert d["counts"] == {"love": 1}
    r = client.post("/react", data={"target": "post", "target_id": pid,
                                    "emoji": "love"}, headers=JSON)
    d = r.json()
    assert d["ok"] and not d["active"] and d["my_reaction"] is None
    assert d["counts"] == {}


def test_react_switch_emoji_keeps_one_row():
    pid = _mk_post("rxn_bob", "rxn switch post")
    client.post("/react", data={"target": "post", "target_id": pid,
                                "emoji": "love"}, headers=JSON)
    r = client.post("/react", data={"target": "post", "target_id": pid,
                                    "emoji": "laugh"}, headers=JSON)
    d = r.json()
    assert d["ok"] and d["active"] and d["my_reaction"] == "laugh"
    assert d["counts"] == {"laugh": 1}
    rows = asyncio.run(_db_all(
        "SELECT emoji FROM reactions WHERE target='post' AND target_id=?", (pid,)))
    assert [r[0] for r in rows] == ["laugh"]


def test_react_comment():
    pid = _mk_post("rxn_cara", "rxn comment post")
    r = client.post(f"/post/{pid}/comment", data={"body": "rxn nice reply"})
    assert r.status_code in (200, 303), r.status_code
    cid = _comment_id("rxn nice reply")
    r = client.post("/react", data={"target": "comment", "target_id": cid,
                                    "emoji": "laugh"}, headers=JSON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] and d["active"] and d["counts"] == {"laugh": 1}
    # like works on comments too
    r = client.post("/react", data={"target": "comment", "target_id": cid,
                                    "emoji": "like"}, headers=JSON)
    assert r.json()["counts"] == {"like": 1}


def test_react_validation():
    pid = _mk_post("rxn_dan", "rxn validation post")
    r = client.post("/react", data={"target": "post", "target_id": pid,
                                    "emoji": "poop"}, headers=JSON)
    assert r.status_code == 400
    r = client.post("/react", data={"target": "dm", "target_id": pid,
                                    "emoji": "like"}, headers=JSON)
    assert r.status_code == 400
    r = client.post("/react", data={"target": "post", "target_id": 999999,
                                    "emoji": "like"}, headers=JSON)
    assert r.status_code == 404
    r = client.post("/react", data={"target": "comment", "target_id": 999999,
                                    "emoji": "like"}, headers=JSON)
    assert r.status_code == 404


def test_legacy_like_route_still_works():
    pid = _mk_post("rxn_erin", "rxn legacy like post")
    r = client.post(f"/post/{pid}/like")
    assert r.status_code in (200, 303), r.status_code
    r = client.post("/react", data={"target": "post", "target_id": pid,
                                    "emoji": "like"}, headers=JSON)
    d = r.json()
    # /like turned it on, so this /react toggles it back off
    assert d["ok"] and not d["active"] and d["counts"] == {}


def test_feed_renders_reaction_markers():
    pid = _mk_post("rxn_finn", "rxn feed markers post")
    client.post("/react", data={"target": "post", "target_id": pid,
                                "emoji": "wow"}, headers=JSON)
    r = client.post(f"/post/{pid}/comment", data={"body": "rxn marker reply"})
    assert r.status_code in (200, 303)
    cid = _comment_id("rxn marker reply")
    client.post("/react", data={"target": "comment", "target_id": cid,
                                "emoji": "love"}, headers=JSON)
    r = client.get("/feed")
    assert r.status_code == 200
    html = r.text
    assert f'id="rsum-post-{pid}"' in html
    assert f'id="rsum-comment-{cid}"' in html
    assert f'id="rpick-post-{pid}"' in html
    assert f'id="rpick-comment-{cid}"' in html
    assert "\U0001F62E" in html and "\u2764\uFE0F" in html


def test_delete_post_clears_reactions():
    pid = _mk_post("rxn_gus", "rxn delete post")
    client.post("/react", data={"target": "post", "target_id": pid,
                                "emoji": "sad"}, headers=JSON)
    r = client.post(f"/post/{pid}/comment", data={"body": "rxn doomed reply"})
    assert r.status_code in (200, 303)
    cid = _comment_id("rxn doomed reply")
    client.post("/react", data={"target": "comment", "target_id": cid,
                                "emoji": "angry"}, headers=JSON)
    r = client.post(f"/post/{pid}/delete")
    assert r.status_code in (200, 303), r.status_code
    rows = asyncio.run(_db_all(
        "SELECT id FROM reactions WHERE (target='post' AND target_id=?)"
        " OR (target='comment' AND target_id=?)", (pid, cid)))
    assert rows == []
