"""Knight & Crown game hub (/game) tests.

Covers the hub page (login gate, live widgets, fail-soft offline mode),
heir enlistment, raid signup, the @knightandcrown game feed, and the
World Tips auto-poster. The resonance backend is faked via monkeypatch —
no network in tests.
"""
import asyncio
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="thc_test_game_")
os.environ["THC_SOCIAL_DB"] = os.path.join(_tmp, "test.db")
os.environ["THC_SOCIAL_MEDIA"] = os.path.join(_tmp, "media")
# StaticFiles requires the directory to exist at app import time.
os.makedirs(os.path.join(_tmp, "media", "avatars"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "posts"), exist_ok=True)

import aiosqlite  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.main import app  # noqa: E402
from app.auth import AGE_COOKIE, make_age_token, hash_password  # noqa: E402
from app.db import DB_PATH, init_db  # noqa: E402

asyncio.run(init_db())
client = TestClient(app)
# The 21+ age gate fronts every page; tests run as a verified adult.
client.cookies.set(AGE_COOKIE, make_age_token())


def _register(username, password="secret123"):
    return client.post("/register", data={
        "username": username, "display_name": username.title(),
        "password": password, "twitch_username": username,
        "agree_terms": "yes"},
        follow_redirects=False)


def _login(username, password="secret123"):
    return client.post("/login", data={"username": username,
                                       "password": password},
                       follow_redirects=False)


async def _mk_game_user():
    db = await aiosqlite.connect(DB_PATH)
    await db.execute(
        "INSERT OR IGNORE INTO users (username, display_name, password_hash,"
        " bio, twitch_username, avatar_approved, is_admin, created_at)"
        " VALUES ('knightandcrown', 'Knight & Crown', ?, 'game', '', 1, 0, 'now')",
        (hash_password("x"),))
    await db.commit()
    await db.close()


async def _mk_heir(username, player_id=7, token="tok123"):
    db = await aiosqlite.connect(DB_PATH)
    cur = await db.execute("SELECT id FROM users WHERE username = ?",
                           (username,))
    uid = (await cur.fetchone())[0]
    await db.execute(
        "INSERT OR IGNORE INTO game_heirs (user_id, player_id, player_token,"
        " heir_name, antenna, polymath, created_at)"
        " VALUES (?, ?, ?, 'TestHeir', 'saxis', 'technical', 'now')",
        (uid, player_id, token))
    await db.commit()
    await db.close()


def _fake_world():
    return {
        "tide": {"height_ft": 2.1, "root_exposed": True},
        "antenna": {"antennas": {
            "11:11": {"open": True, "strength": 0.9, "tint": "moon-blue"},
            "5:55": {"open": False, "strength": 0.0, "tint": "marsh-gold"},
        }},
        "ek": {"ek": 0.87, "ek_high": True, "threshold": 0.85},
        "raid_miracle": False,
    }


def _fake_raid():
    return {"vow_day": "2028-04-20", "raid_time": "2028-04-20T10:14:00+00:00",
            "cycle_id": 3, "status": "open", "raid_size": 20,
            "miracle": "all antennas open 11:11 and 5:55 at once",
            "tint": "bicentennial red white blue"}


def _use_fakes(monkeypatch):
    async def fake_world():
        return _fake_world()

    async def fake_peaks():
        return {"peaks": [{"id": 1, "ts": "2028-04-20T10:15:00+00:00",
                           "session_id": 9, "player_ids": [7],
                           "ek_value": 0.91, "sync_score": 92}]}

    async def fake_raid():
        return _fake_raid()

    async def fake_cycle(cid):
        assert cid == 3
        return {"cycle_id": 3, "status": "open", "roster": [7],
                "roster_count": 1, "raid_size": 20}

    monkeypatch.setattr(main, "get_game_world", fake_world)
    monkeypatch.setattr(main, "get_game_peaks", fake_peaks)
    monkeypatch.setattr(main, "get_game_raid", fake_raid)
    monkeypatch.setattr(main, "get_game_cycle", fake_cycle)


# ---------------------------------------------------------------- page
def test_game_requires_login():
    client.post("/logout", follow_redirects=False)
    r = client.get("/game", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_game_hub_backend_offline():
    _register("gamefan1")
    _login("gamefan1")
    main._game_cache.clear()
    old = main.KHAT_BACKEND_URL
    main.KHAT_BACKEND_URL = "http://127.0.0.1:9"  # dead port: fail-soft path
    try:
        r = client.get("/game")
        assert r.status_code == 200
        assert "KNIGHT &amp; CROWN" in r.text or "KNIGHT & CROWN" in r.text
        assert "He was left where we&#39;d find him." in r.text or \
            "He was left where we'd find him." in r.text
        assert "signal lost" in r.text
        assert "No heirs yet" in r.text
        assert "No peaks logged yet" in r.text
    finally:
        main.KHAT_BACKEND_URL = old
        main._game_cache.clear()


def test_game_hub_live_widgets(monkeypatch):
    _register("gamefan2")
    _login("gamefan2")
    _use_fakes(monkeypatch)
    r = client.get("/game")
    assert r.status_code == 200
    assert "2.1 ft" in r.text
    assert "ROOT EXPOSED" in r.text
    assert "Saxis Technicals" in r.text
    assert "OPEN" in r.text
    assert "0.870" in r.text
    assert "CYAN" in r.text
    assert "April 20, 2028" in r.text
    assert "1/20 heirs signed" in r.text
    assert "0.910" in r.text  # peak leaderboard
    assert "sync 92" in r.text


# ---------------------------------------------------------------- enlist
def test_enlist_creates_heir(monkeypatch):
    _register("heirbob")
    _login("heirbob")

    async def fake_create(name, faction, polymath):
        assert name == "Dunreath"
        assert faction == "dunreef"
        assert polymath == "warden"
        return (201, {"id": 42, "name": name, "player_token": "tok42"})

    monkeypatch.setattr(main, "khat_create_player", fake_create)
    r = client.post("/game/enlist",
                    data={"heir_name": "Dunreath", "antenna": "dunreef",
                          "polymath": "warden"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "Heir+created" in r.headers["location"]
    row = asyncio.run(_heir_row("heirbob"))
    assert row is not None and row[2] == 42 and row[3] == "tok42"
    # hub now shows the heir instead of the enlist form
    _use_fakes(monkeypatch)
    page = client.get("/game")
    assert "Dunreath" in page.text
    assert "Dun Reef Wardens" in page.text


async def _heir_row(username):
    db = await aiosqlite.connect(DB_PATH)
    cur = await db.execute(
        """SELECT h.id, h.user_id, h.player_id, h.player_token, h.heir_name,
                  h.antenna, h.polymath FROM game_heirs h
           JOIN users u ON u.id = h.user_id WHERE u.username = ?""",
        (username,))
    row = await cur.fetchone()
    await db.close()
    return row


def test_enlist_rejects_bad_input():
    _register("heirbad")
    _login("heirbad")
    r = client.post("/game/enlist",
                    data={"heir_name": "X", "antenna": "mordor",
                          "polymath": "wizard"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "Pick+a+name" in r.headers["location"]
    assert asyncio.run(_heir_row("heirbad")) is None


def test_enlist_only_one_heir_per_user(monkeypatch):
    _register("heironce")
    _login("heironce")

    async def fake_create(name, faction, polymath):
        return (201, {"id": 43, "player_token": "tok43"})

    monkeypatch.setattr(main, "khat_create_player", fake_create)
    client.post("/game/enlist",
                data={"heir_name": "First", "antenna": "saxis",
                      "polymath": "technical"},
                follow_redirects=False)
    r = client.post("/game/enlist",
                    data={"heir_name": "Second", "antenna": "crysfeld",
                          "polymath": "crown"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "already+have+an+heir" in r.headers["location"]
    db = asyncio.run(_heir_count("heironce"))
    assert db == 1


async def _heir_count(username):
    db = await aiosqlite.connect(DB_PATH)
    cur = await db.execute(
        "SELECT COUNT(*) FROM game_heirs h JOIN users u ON u.id = h.user_id"
        " WHERE u.username = ?", (username,))
    n = (await cur.fetchone())[0]
    await db.close()
    return n


def test_enlist_backend_down(monkeypatch):
    _register("heirdown")
    _login("heirdown")

    async def fake_down(name, faction, polymath):
        return (0, {"detail": "resonance backend unreachable"})

    monkeypatch.setattr(main, "khat_create_player", fake_down)
    r = client.post("/game/enlist",
                    data={"heir_name": "Nope", "antenna": "saxis",
                          "polymath": "technical"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "unreachable" in r.headers["location"]
    assert asyncio.run(_heir_row("heirdown")) is None


# ---------------------------------------------------------------- raid join
def test_raid_join_signs_up_heir(monkeypatch):
    _register("raidfan")
    _login("raidfan")
    asyncio.run(_mk_heir("raidfan", player_id=7, token="tok123"))
    calls = []

    async def fake_raid():
        return _fake_raid()

    async def fake_join(cycle_id, player_id, token):
        calls.append((cycle_id, player_id, token))
        return (200, {"cycle_id": cycle_id, "player_id": player_id,
                      "roster": 2, "raid_size": 20})

    monkeypatch.setattr(main, "get_game_raid", fake_raid)
    monkeypatch.setattr(main, "khat_join_raid", fake_join)
    r = client.post("/game/raid/join", follow_redirects=False)
    assert r.status_code == 303
    assert "Signed+up" in r.headers["location"]
    assert calls == [(3, 7, "tok123")]


def test_raid_join_requires_heir():
    _register("noraidheir")
    _login("noraidheir")
    r = client.post("/game/raid/join", follow_redirects=False)
    assert r.status_code == 303
    assert "Enlist+an+heir+first" in r.headers["location"]


# ---------------------------------------------------------------- game feed + tips
def test_game_feed_shows_game_posts(monkeypatch):
    _register("feedfan")
    _login("feedfan")
    asyncio.run(_mk_game_user())

    async def post_one():
        db = await _db()
        try:
            return await main.post_as_game(
                db, "🌊 World Tip — test dispatch #knightandcrown")
        finally:
            await db.close()

    pid = asyncio.run(post_one())
    assert pid is not None
    _use_fakes(monkeypatch)
    r = client.get("/game")
    assert r.status_code == 200
    assert "World Tip — test dispatch" in r.text


async def _db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


def test_world_tip_posts_once_per_window(monkeypatch):
    asyncio.run(_mk_game_user())
    _use_fakes(monkeypatch)

    async def run_tip():
        db = await _db()
        try:
            await main.maybe_post_game_tip(db, _fake_world())
        finally:
            await db.close()

    n0 = asyncio.run(_tip_count())
    asyncio.run(run_tip())
    n1 = asyncio.run(_tip_count())
    asyncio.run(run_tip())  # second call: fully deduped by meta keys
    n2 = asyncio.run(_tip_count())
    assert n2 == n1  # no duplicates on repeat
    assert n1 - n0 <= 3  # at most one post per event type


async def _tip_count():
    db = await aiosqlite.connect(DB_PATH)
    cur = await db.execute(
        "SELECT COUNT(*) FROM posts p JOIN users u ON u.id = p.user_id"
        " WHERE u.username = 'knightandcrown' AND p.body LIKE '🌊 World Tip%'")
    n = (await cur.fetchone())[0]
    await db.close()
    return n


def test_countdown_math():
    assert main._game_countdown("not-a-date") is None
    d, h, m = main._game_countdown("2028-04-20T10:14:00+00:00")
    assert d > 500  # ~2.5 years out from the 2026 test "now"
    assert 0 <= h < 24 and 0 <= m < 60
