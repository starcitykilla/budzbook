"""One-time BudzBook setup: jarvis/Budz Picks account + BMG employee badges.

Run once from the project root:
    .venv\\Scripts\\python.exe bmg_setup.py

- Creates the `jarvis` user (display name "Budz Picks") with a generated
  avatar, if it doesn't exist yet.
- Gives jarvis follows to krzy_budz + queenshida.
- Sets badge='bmg' on jarvis, krzy_budz, queenshida.
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
BMG_CREW = ("jarvis", "krzy_budz", "queenshida")


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
        cur = await db.execute("SELECT id FROM users WHERE username = 'jarvis'")
        row = await cur.fetchone()
        if row:
            jarvis_id = row["id"]
            print("jarvis already exists (id=%s)" % jarvis_id)
        else:
            pw = secrets.token_urlsafe(16)  # system account; nobody logs in as jarvis
            await db.execute(
                """INSERT INTO users
                   (username, display_name, password_hash, bio, twitch_username,
                    avatar_approved, is_admin, created_at)
                   VALUES (?, ?, ?, ?, '', 1, 0, ?)""",
                ("jarvis", "Budz Picks", hash_password(pw), JARVIS_BIO, now))
            cur = await db.execute("SELECT id FROM users WHERE username = 'jarvis'")
            jarvis_id = (await cur.fetchone())["id"]
            make_jarvis_avatar()
            for target in ("krzy_budz", "queenshida"):
                cur = await db.execute("SELECT id FROM users WHERE username = ?", (target,))
                t = await cur.fetchone()
                if t:
                    await db.execute(
                        "INSERT OR IGNORE INTO follows (follower_id, followed_id, created_at)"
                        " VALUES (?, ?, ?)", (jarvis_id, t["id"], now))
            print("created jarvis (Budz Picks), id=%s" % jarvis_id)
        await db.execute(
            "UPDATE users SET badge = 'bmg' WHERE username IN ('jarvis', 'krzy_budz', 'queenshida')")
        await db.commit()
        cur = await db.execute(
            "SELECT username, display_name, badge FROM users WHERE badge = 'bmg'")
        for r in await cur.fetchall():
            print("badged: @%s (%s)" % (r["username"], r["display_name"]))
    print("BMG setup complete.")


if __name__ == "__main__":
    asyncio.run(main())
