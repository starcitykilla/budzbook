"""Adapter layer between BudzBook site and Brad's T.H.C. stream systems.

V1 STATUS: **everything here returns MOCK data** so the site works standalone
without Brad's stream PC. Each function documents exactly what to replace to
go live:

* Casino leaderboard -> LIVE: reads the real casino SQLite DB
  (thc-test/cannabet.db, written by ``the_botanist.py``). Falls back to
  the mock board below if the DB can't be read.
* Sportsbook lines   -> import ``sportsbook.get_all_games()`` from the T.H.C.
  codebase (same code that powers the ``$lines`` chat command) instead of the
  mock board below.
* Grow stats         -> read the T.H.C. grow-monitor data (sensor readings /
  plant photos) instead of the mock tent summary.

The page routes in ``main.py`` only ever talk to these functions, so swapping
mock -> real is a one-file change.
"""

import os
import sqlite3


# Flip to False once real data sources are wired up; pages show a
# "demo data" badge while this is True.
MOCK = True


def _casino_db_path():
    # Env override keeps tests hermetic; default is the bot's live casino DB.
    return os.environ.get("CASINO_DB", r"C:\Users\Starc\thc-test\cannabet.db")


def _mock_leaderboard(limit: int = 10):
    board = [
        {"username": "krzy_budz", "budz": 2090, "wins": 34},
        {"username": "queenshida", "budz": 1875, "wins": 28},
        {"username": "jarvis", "budz": 1750, "wins": 19},
        {"username": "powme0wpow", "budz": 1520, "wins": 21},
        {"username": "hoodzwtf", "budz": 1210, "wins": 17},
        {"username": "budtender_fan", "budz": 940, "wins": 12},
        {"username": "growmie42", "budz": 610, "wins": 8},
    ]
    return board[:limit]


def get_casino_leaderboard(limit: int = 10):
    """Top casino players by real chat-casino balance.

    Returns (board, live). Live reads the T.H.C. bot's casino DB
    (cannabet.db, written by the_botanist.py). The casino engine doesn't
    track win counts, so live rows carry wins=None (rendered as "--").
    On any read error it falls back to the mock board so the page never
    breaks.
    """
    try:
        conn = sqlite3.connect(_casino_db_path(), timeout=5)
        try:
            rows = conn.execute(
                "SELECT username, balance FROM users ORDER BY balance DESC LIMIT ?",
                (limit,)).fetchall()
        finally:
            conn.close()
        return ([{"username": u, "budz": b, "wins": None} for u, b in rows], True)
    except Exception:
        return _mock_leaderboard(limit), False


def get_sportsbook_lines():
    """Budz Book lines board, grouped by sport.

    LIVE VERSION: ``from sportsbook import get_all_games`` (T.H.C. repo) and
    reshape each game into the dict shape below.
    Return: {"NFL": [game, ...], "MLB": [...], ...}
    game = {"away": str, "home": str, "ml_away": int, "ml_home": int,
            "spread": str, "total": str}
    """
    return {
        "NFL": [
            {"away": "Detroit Lions", "home": "Buffalo Bills",
             "ml_away": 188, "ml_home": -225, "spread": "BUF -4.5", "total": "O/U 53.5"},
            {"away": "Carolina Panthers", "home": "Atlanta Falcons",
             "ml_away": -142, "ml_home": 120, "spread": "CAR -2.5", "total": "O/U 43.5"},
            {"away": "Dallas Cowboys", "home": "Washington Commanders",
             "ml_away": -205, "ml_home": 172, "spread": "DAL -3.5", "total": "O/U 50.5"},
        ],
        "MLB": [
            {"away": "New York Yankees", "home": "Boston Red Sox",
             "ml_away": -130, "ml_home": 110, "spread": "NYY -1.5", "total": "O/U 9.0"},
        ],
    }


def get_grow_stats():
    """Summary of the grow tent for the Grow page header.

    LIVE VERSION: read the T.H.C. grow-monitor readings (temperature,
    humidity, light cycle, plant age) instead of these static values.
    """
    return {
        "strain": "Mystery Kush",
        "day": 34,
        "temp_f": 76.5,
        "humidity": 58,
        "light_cycle": "18/6",
        "note": "Demo readings — wire up the real tent sensors to go live.",
    }


# --------------------------------------------------------------------------
# Game-currency bridge (bot -> site). The Twitch bot calls this to pay out
# watch-time and betting rewards into BudzBook wallets. Runs against the
# site's SQLite DB (same machine when the site is hosted on the stream PC).
# --------------------------------------------------------------------------
def award_currency(username: str, code: str, amount: int, reason: str) -> int:
    """Credit game currency to a site user. Returns the new balance."""
    import asyncio
    import aiosqlite
    from .currency import award as _award
    from .db import DB_PATH, init_db

    async def _run():
        await init_db()
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        try:
            cur = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
            row = await cur.fetchone()
            if not row:
                raise ValueError(f"unknown user {username}")
            return await _award(db, row["id"], code, amount, reason)
        finally:
            await db.close()

    return asyncio.run(_run())
