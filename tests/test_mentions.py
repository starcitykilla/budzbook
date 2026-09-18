"""Tests for @mention autocomplete + linkified mentions."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="bb_mentions_test_")
os.environ["THC_SOCIAL_DB"] = os.path.join(_tmp, "test.db")
os.environ["THC_SOCIAL_MEDIA"] = os.path.join(_tmp, "media")
os.makedirs(os.path.join(_tmp, "media", "avatars"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "posts"), exist_ok=True)

import aiosqlite  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import DB_PATH, init_db  # noqa: E402
from app.main import app, linkify_tags, _clear_mention_cache  # noqa: E402
from app.auth import make_age_token, AGE_COOKIE  # noqa: E402

asyncio.run(init_db())
client = TestClient(app, raise_server_exceptions=False)
client.cookies.set(AGE_COOKIE, make_age_token())


def _register(username, display_name=None, password="pw123456"):
    r = client.post("/register", data={"username": username, "password": password,
                                       "display_name": display_name or username})
    assert r.status_code in (200, 303), (username, r.status_code, r.text[:200])


def _login(username, password="pw123456"):
    r = client.post("/login", data={"username": username, "password": password})
    assert r.status_code in (200, 303), (username, r.status_code)


def _fresh_client():
    c = TestClient(app, raise_server_exceptions=False)
    c.cookies.set(AGE_COOKIE, make_age_token())
    return c


# ---- /api/users/search ----
def test_search_requires_login():
    c = _fresh_client()
    r = c.get("/api/users/search?q=bob")
    assert r.status_code == 401
    assert r.json()["error"] == "login required"


def test_search_prefix_matching():
    _register("mtn_brad", display_name="Bradley Knight")
    _register("mtn_shida", display_name="Shida")
    _register("mtn_other", display_name="Somebody Else")
    _clear_mention_cache()
    _login("mtn_brad")
    r = client.get("/api/users/search?q=mtn_b")
    assert r.status_code == 200
    names = [u["username"] for u in r.json()["results"]]
    assert "mtn_brad" in names
    assert "mtn_other" not in names
    # display-name prefix match
    r = client.get("/api/users/search?q=Bradley")
    names = [u["username"] for u in r.json()["results"]]
    assert "mtn_brad" in names
    # case-insensitive
    r = client.get("/api/users/search?q=MTN_S")
    names = [u["username"] for u in r.json()["results"]]
    assert "mtn_shida" in names


def test_search_no_private_fields():
    _login("mtn_brad")
    r = client.get("/api/users/search?q=mtn_")
    assert r.status_code == 200
    for u in r.json()["results"]:
        assert set(u.keys()) == {"id", "username", "display_name", "avatar_url"}, u.keys()


def test_search_empty_and_limit():
    _login("mtn_brad")
    r = client.get("/api/users/search?q=")
    assert r.status_code == 200 and r.json()["results"] == []
    for i in range(10):
        _register(f"mtn_flood{i:02d}")
    _clear_mention_cache()
    r = client.get("/api/users/search?q=mtn_flood")
    assert r.status_code == 200
    assert len(r.json()["results"]) == 8


def test_search_like_metachars_safe():
    _login("mtn_brad")
    r = client.get("/api/users/search?q=%")
    assert r.status_code == 200
    # % must be treated literally, not as a wildcard over every user
    assert r.json()["results"] == []


# ---- linkification ----
def test_linkify_mention_existing_user():
    _register("mtn_mentioned")
    _clear_mention_cache()
    html = str(linkify_tags("hey @mtn_mentioned what's up"))
    assert '<a class="mention" href="/u/mtn_mentioned">@mtn_mentioned</a>' in html


def test_linkify_mention_nonexistent_left_alone():
    _clear_mention_cache()
    html = str(linkify_tags("hey @nosuchuserhere whats up"))
    assert "@nosuchuserhere" in html
    assert 'class="mention"' not in html


def test_linkify_mention_email_not_linked():
    _register("mtn_mailbox")
    _clear_mention_cache()
    html = str(linkify_tags("write to x@mtn_mailbox.com please"))
    assert 'class="mention"' not in html
    assert "@mtn_mailbox" in html


def test_linkify_mention_xss_safe():
    _register("mtn_xss")
    _clear_mention_cache()
    html = str(linkify_tags('<script>alert(1)</script> @mtn_xss "quoted"'))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&#34;quoted&#34;" in html
    assert 'href="/u/mtn_xss"' in html


def test_linkify_hashtag_still_works():
    _clear_mention_cache()
    html = str(linkify_tags("loving #budz tonight"))
    assert 'href="/tag/budz"' in html
    assert "#budz" in html


def test_linkify_mention_and_hashtag_together():
    _register("mtn_combo")
    _clear_mention_cache()
    html = str(linkify_tags("@mtn_combo brought the #dank"))
    assert 'href="/u/mtn_combo"' in html
    assert 'href="/tag/dank"' in html


# ---- end to end: comment rendering ----
def test_comment_mention_renders_as_link():
    _register("mtn_poster")
    _register("mtn_target")
    _clear_mention_cache()
    _login("mtn_poster")
    r = client.post("/post", data={"body": "hello world"})
    assert r.status_code in (200, 303)
    # find the new post id from the feed
    import re
    r = client.get("/feed")
    m = re.search(r'id="post-(\d+)"', r.text)
    assert m, "no post rendered"
    pid = m.group(1)
    r = client.post(f"/post/{pid}/comment", data={"body": "nice one @mtn_target!"})
    assert r.status_code in (200, 303)
    r = client.get("/feed")
    assert '<a class="mention" href="/u/mtn_target">@mtn_target</a>' in r.text
