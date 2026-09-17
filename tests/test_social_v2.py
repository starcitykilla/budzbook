"""Tests for BudzBook social v2: hashtags/trending, groups, video posts, go-live dedupe."""
import asyncio
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="bb_social2_test_")
os.environ["THC_SOCIAL_DB"] = os.path.join(_tmp, "test.db")
os.environ["THC_SOCIAL_MEDIA"] = os.path.join(_tmp, "media")
os.makedirs(os.path.join(_tmp, "media", "avatars"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "posts"), exist_ok=True)

import aiosqlite  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import DB_PATH, init_db  # noqa: E402
from app.main import app, extract_hashtags, linkify_tags  # noqa: E402
from app.auth import make_age_token, AGE_COOKIE  # noqa: E402

asyncio.run(init_db())
client = TestClient(app, raise_server_exceptions=False)
client.cookies.set(AGE_COOKIE, make_age_token())


def _register(username, password="pw123456"):
    r = client.post("/register", data={"username": username, "password": password,
                                       "display_name": username})
    assert r.status_code in (200, 303), (username, r.status_code)


def _login(username, password="pw123456"):
    r = client.post("/login", data={"username": username, "password": password})
    assert r.status_code in (200, 303), (username, r.status_code)


# ---- hashtag unit tests ----
def test_extract_hashtags():
    assert extract_hashtags("Loving #Indica tonight #indica") == {"indica"}
    assert extract_hashtags("no tags here") == set()
    assert extract_hashtags("#Grow #grow2 #GROW") == {"grow", "grow2"}


def test_linkify_escapes_and_links():
    html = str(linkify_tags('<script>alert(1)</script> hi #budz'))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert 'href="/tag/budz"' in html
    assert "#budz" in html


# ---- hashtags via posting ----
def test_post_creates_hashtags_and_trending():
    _register("tagger")
    _login("tagger")
    r = client.post("/post", data={"body": "First #harvest of the year #Dank"})
    assert r.status_code in (200, 303)
    r = client.get("/trending")
    assert r.status_code == 200
    assert "#harvest" in r.text and "#dank" in r.text
    r = client.get("/tag/harvest")
    assert r.status_code == 200
    assert "First" in r.text
    # hashtag link appears in rendered post
    assert 'href="/tag/harvest"' in r.text


# ---- groups ----
def test_groups_create_join_post():
    _register("grouper")
    _login("grouper")
    r = client.post("/groups", data={"name": "Test Budz", "description": "a test group"})
    assert r.status_code in (200, 303)
    r = client.get("/groups")
    assert r.status_code == 200 and "Test Budz" in r.text
    # get the group id from the page
    import re
    m = re.search(r"/groups/(\d+)", r.text)
    assert m
    gid = m.group(1)
    # post to the group
    r = client.post("/post", data={"body": "Hello group #sesh", "group_id": gid})
    assert r.status_code in (200, 303)
    r = client.get(f"/groups/{gid}")
    assert r.status_code == 200
    assert "Hello group" in r.text
    # group badge shows on the post
    assert "Test Budz" in r.text
    # non-member cannot post
    _register("outsider")
    _login("outsider")
    r = client.post("/post", data={"body": "sneaky", "group_id": gid})
    assert r.status_code in (200, 303)
    _login("grouper")
    r = client.get(f"/groups/{gid}")
    assert "sneaky" not in r.text
    # outsider joins, then posts
    _login("outsider")
    r = client.post(f"/groups/{gid}/join")
    assert r.status_code in (200, 303)
    r = client.post("/post", data={"body": "now I can post", "group_id": gid})
    assert r.status_code in (200, 303)
    r = client.get(f"/groups/{gid}")
    assert "now I can post" in r.text
    # leave
    r = client.post(f"/groups/{gid}/leave")
    assert r.status_code in (200, 303)


# ---- video upload validation ----
def test_video_upload_rejected_bad_ext():
    _register("clipper")
    _login("clipper")
    r = client.post("/post", data={"body": "try exe"},
                    files={"video": ("evil.exe", io.BytesIO(b"nope"), "application/octet-stream")})
    # rejected -> redirect with message, no video post created
    assert r.status_code in (200, 303)
    r = client.get("/clips")
    assert r.status_code == 200
    assert "evil" not in r.text


def test_video_upload_accepted_mp4():
    _login("clipper")
    fake_mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100
    r = client.post("/post", data={"body": "my first clip #clips"},
                    files={"video": ("clip.mp4", io.BytesIO(fake_mp4), "video/mp4")})
    assert r.status_code in (200, 303)
    r = client.get("/clips")
    assert r.status_code == 200
    assert "my first clip" in r.text
    assert "<video" in r.text


# ---- go-live dedupe (no network: verify table + idempotency logic) ----
def test_golive_dedupe_table():
    async def _check():
        db = await aiosqlite.connect(DB_PATH)
        await db.execute(
            "INSERT OR IGNORE INTO golive_posts (stream_id, post_id, created_at)"
            " VALUES ('s1', 1, 't')")
        await db.commit()
        cur = await db.execute("SELECT COUNT(*) FROM golive_posts WHERE stream_id = 's1'")
        n = (await cur.fetchone())[0]
        # second insert of same stream is ignored
        await db.execute(
            "INSERT OR IGNORE INTO golive_posts (stream_id, post_id, created_at)"
            " VALUES ('s1', 2, 't')")
        await db.commit()
        cur = await db.execute("SELECT post_id FROM golive_posts WHERE stream_id = 's1'")
        pid = (await cur.fetchone())[0]
        await db.close()
        return n, pid
    n, pid = asyncio.run(_check())
    assert n == 1 and pid == 1


# ---- nav + pages render ----
def test_new_pages_render():
    _login("tagger")
    for path in ("/trending", "/groups", "/clips"):
        r = client.get(path)
        assert r.status_code == 200, path
    r = client.get("/feed")
    assert "Trending" in r.text and "Groups" in r.text and "Clips" in r.text
