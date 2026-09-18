"""One-time BudzBook setup: jarvis/Budz Picks account + knightandcrown game
account + BMG employee badges.

Run once from the project root:
    .venv\\Scripts\\python.exe bmg_setup.py

- Creates the `jarvis` user (display name "Budz Picks") with a generated
  avatar, if it doesn't exist yet.
- Creates the `knightandcrown` user (display name "Knight & Crown") with a
  generated avatar, if it doesn't exist yet. This is the game account:
  World Tips and Crown Translations post from here, and it owns the
  game feed on /game.
- Gives jarvis follows to krzy_budz + queenshida.
- Sets badge='bmg' on jarvis, krzy_budz, queenshida, knightandcrown.
Safe to re-run (idempotent).
"""
import asyncio
import os
import secrets
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import aiosqlite
from PIL import Image, ImageDraw, ImageFont

from app.auth import hash_password
from app.db import AVATAR_DIR, DB_PATH, avatar_path_for, init_db

JARVIS_BIO = (
    "I am Jarvis — Budz's AI co-pilot and official Blindman Gaming employee. "
    "Every morning I post my Budz Picks: leans and parlay tickets for the day's "
    "slate. Check /picks for today's sheet. Play money only, vibes always."
)
BMG_CREW = ("jarvis", "krzy_budz", "queenshida", "knightandcrown")

GAME_BIO = (
    "KNIGHT & CROWN — Two Shores. He was left where we'd find him. "
    "Blindman Gaming's waterman fantasy MMO. World Tips and Crown "
    "Translations post here. The Sound is the medium; Dun Reef is the lens."
)


def make_game_avatar() -> None:
    """Moon-blue / marsh-gold split roundel with K&C monogram (original art)."""
    img = Image.new("RGB", (512, 512), (10, 14, 12))
    d = ImageDraw.Draw(img)
    d.ellipse([56, 56, 456, 456], fill=(127, 180, 255))       # moon-blue
    d.pieslice([56, 56, 456, 456], start=90, end=270,
               fill=(232, 193, 90))                            # marsh-gold half
    d.ellipse([150, 150, 362, 362], fill=(10, 14, 12))
    try:
        font = ImageFont.load_default(size=110)
    except TypeError:  # very old Pillow
        font = ImageFont.load_default()
    d.text((256, 256), "K&C", fill=(232, 193, 90), font=font, anchor="mm")
    os.makedirs(AVATAR_DIR, exist_ok=True)
    img.save(avatar_path_for("knightandcrown"), "JPEG", quality=88)


async def ensure_user(db, username, display_name, bio, avatar_fn, now):
    """Idempotent system-user creation. Returns the user id."""
    cur = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = await cur.fetchone()
    if row:
        print("%s already exists (id=%s)" % (username, row["id"]))
        return row["id"]
    pw = secrets.token_urlsafe(16)  # system account; nobody logs in as it
    await db.execute(
        """INSERT INTO users
           (username, display_name, password_hash, bio, twitch_username,
            avatar_approved, is_admin, created_at)
           VALUES (?, ?, ?, ?, '', 1, 0, ?)""",
        (username, display_name, hash_password(pw), bio, now))
    cur = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
    uid = (await cur.fetchone())["id"]
    avatar_fn()
    print("created %s (%s), id=%s" % (username, display_name, uid))
    return uid


def make_jarvis_avatar() -> None:
    """Gold circle + B on dark, matching the seed avatar style."""
    img = Image.new("RGB", (512, 512), (13, 21, 18))
    d = ImageDraw.Draw(img)
    d.ellipse([56, 56, 456, 456], fill=(212, 175, 55))
    try:
        font = ImageFont.load_default(size=220)
    except TypeError:  # very old Pillow
        font = ImageFont.load_default()
    d.text((256, 256), "B", fill=(6, 17, 11), font=font, anchor="mm")
    os.makedirs(AVATAR_DIR, exist_ok=True)
    img.save(avatar_path_for("jarvis"), "JPEG", quality=88)


async def main() -> None:
    await init_db()  # creates tables + runs migrations (badge column)
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        jarvis_id = await ensure_user(db, "jarvis", "Budz Picks", JARVIS_BIO,
                                      make_jarvis_avatar, now)
        for target in ("krzy_budz", "queenshida"):
            cur = await db.execute("SELECT id FROM users WHERE username = ?", (target,))
            t = await cur.fetchone()
            if t:
                await db.execute(
                    "INSERT OR IGNORE INTO follows (follower_id, followed_id, created_at)"
                    " VALUES (?, ?, ?)", (jarvis_id, t["id"], now))
        game_id = await ensure_user(db, "knightandcrown", "Knight & Crown",
                                    GAME_BIO, make_game_avatar, now)
        for target in ("krzy_budz", "queenshida"):
            cur = await db.execute("SELECT id FROM users WHERE username = ?", (target,))
            t = await cur.fetchone()
            if t:
                await db.execute(
                    "INSERT OR IGNORE INTO follows (follower_id, followed_id, created_at)"
                    " VALUES (?, ?, ?)", (game_id, t["id"], now))
        await db.execute(
            "UPDATE users SET badge = 'bmg' WHERE username IN "
            "('jarvis', 'krzy_budz', 'queenshida', 'knightandcrown')")
        await db.commit()
        cur = await db.execute(
            "SELECT username, display_name, badge FROM users WHERE badge = 'bmg'")
        for r in await cur.fetchall():
            print("badged: @%s (%s)" % (r["username"], r["display_name"]))
    print("BMG setup complete.")


if __name__ == "__main__":
    asyncio.run(main())
