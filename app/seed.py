"""Seed BudzBook with demo content.

Run once from the project root:  python -m app.seed
(or: .venv/bin/python -m app.seed)

Creates:
  admin / admin123            (moderator account — use this to try the admin view)
  krzy_budz / budz123         (Brad)
  queenshida / shida123       (Shida)
  powme0wpow / pow123
  hoodzwtf / hood123          (avatar uploaded but NOT approved -> shows in mod queue)

Plus follows, posts, likes, comments, a DM thread, a grow-journal entry and
one open report. Safe to re-run: it does nothing if the admin user exists.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import aiosqlite
from PIL import Image, ImageDraw, ImageFont

from .auth import hash_password
from .db import AVATAR_DIR, DB_PATH, SCHEMA, _migrate, avatar_path_for

USERS = [
    # username, password, display_name, twitch, bio, admin, avatar_approved
    ("admin", "admin123", "Club Mod", "", "Keeping the club green and clean.", True, False),
    ("krzy_budz", "budz123", "KRZY BUDZ", "krzy_budz", "Streamer. Budtender. Your host. 🌿", False, True),
    ("queenshida", "shida123", "Queen Shida", "queenshida", "👑 Queen of the club.", False, True),
    ("powme0wpow", "pow123", "Pow Meow Pow", "powme0wpow", "Here for the vibes and the parlays.", False, True),
    ("hoodzwtf", "hood123", "Hoodz WTF", "hoodzwtf", "Lurker turned poster.", False, False),
]

POSTS = [
    ("krzy_budz", "Welcome to BudzBook! This is our spot between streams. Drop a post, talk your parlays, show your grow. 🌿", "post", 5),
    ("queenshida", "First!! 👑 So happy this exists. Who's watching the game tonight?", "post", 26),
    ("powme0wpow", "Cowboys ML was free money, just saying. 💰", "post", 48),
    ("hoodzwtf", "long time lurker, finally made an account. what's good everybody", "post", 90),
    ("krzy_budz", "Day 34 — the ladies are LOVING the new light cycle. Pics soon. 🌱", "grow", 130),
    ("queenshida", "Budz's chat during final 2 minutes should be studied by scientists 😂", "post", 200),
    ("powme0wpow", "My Nugz balance after that parlay hit 📈📈📈", "post", 320),
]

FOLLOWS = [
    ("queenshida", "krzy_budz"), ("powme0wpow", "krzy_budz"), ("hoodzwtf", "krzy_budz"),
    ("krzy_budz", "queenshida"), ("powme0wpow", "queenshida"), ("hoodzwtf", "powme0wpow"),
]

AVATAR_COLORS = [(61, 220, 132), (232, 197, 71), (176, 146, 255), (224, 108, 108), (110, 180, 220)]


def make_avatar(username: str, color, initial: str) -> None:
    """Generate a simple placeholder avatar (green circle + initial)."""
    img = Image.new("RGB", (512, 512), (13, 21, 18))
    d = ImageDraw.Draw(img)
    d.ellipse([56, 56, 456, 456], fill=color)
    try:
        font = ImageFont.load_default(size=220)
    except TypeError:  # very old Pillow
        font = ImageFont.load_default()
    d.text((256, 256), initial.upper(), fill=(6, 17, 11), font=font, anchor="mm")
    img.save(avatar_path_for(username), "JPEG", quality=88)


CURRENCIES = [
    # code, name, owner, icon, rate_to_base, is_base
    ("BUDZ", "Budz", "krzy_budz", "🌿", 1.0, 1),
    ("GAS", "Gas", "dabby_tv", "⛽", 2.0, 0),
    ("TRICH", "Trichomes", "trichome_tess", "💎", 0.5, 0),
]

SHOP_ITEMS = [
    # name, kind, css_class, price (in BUDZ), icon
    ("Golden Leaf Frame", "frame", "frame-gold", 500, "🌟"),
    ("Emerald Ring", "frame", "frame-emerald", 250, "💚"),
    ("Purple Haze Frame", "frame", "frame-purple", 750, "🟣"),
    ("Fire Frame", "frame", "frame-fire", 1000, "🔥"),
    ("Early Bud", "badge", "", 100, "🌱"),
    ("420 Legend", "badge", "", 420, "🌿"),
    ("Diamond Hands", "badge", "", 2000, "💎"),
]

DEMO_BALANCES = [
    # username, code, amount
    ("krzy_budz", "BUDZ", 5000), ("krzy_budz", "GAS", 100), ("krzy_budz", "TRICH", 200),
    ("queenshida", "BUDZ", 1500), ("queenshida", "TRICH", 50),
    ("powme0wpow", "BUDZ", 800), ("powme0wpow", "GAS", 25),
    ("hoodzwtf", "BUDZ", 300),
]


async def seed_currencies(db) -> None:
    """Idempotent: currencies, shop items, demo balances (runs even on re-seed)."""
    now = datetime.now(timezone.utc).isoformat()
    for code, name, owner, icon, rate, is_base in CURRENCIES:
        await db.execute(
            """INSERT OR IGNORE INTO currencies
               (code, name, owner, icon, rate_to_base, is_base, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (code, name, owner, icon, rate, is_base, now))
    cur = await db.execute("SELECT id FROM currencies WHERE code = 'BUDZ'")
    budz_id = (await cur.fetchone())[0]
    for name, kind, css, price, icon in SHOP_ITEMS:
        await db.execute(
            """INSERT OR IGNORE INTO shop_items
               (name, kind, css_class, price, currency_id, icon, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, kind, css, price, budz_id, icon, now))
    # demo balances (only for users that exist)
    for uname, code, amount in DEMO_BALANCES:
        cur = await db.execute("SELECT id FROM users WHERE username = ?", (uname,))
        u = await cur.fetchone()
        cur = await db.execute("SELECT id FROM currencies WHERE code = ?", (code,))
        c = await cur.fetchone()
        if not u or not c:
            continue
        await db.execute(
            """INSERT OR IGNORE INTO wallets (user_id, currency_id, balance)
               VALUES (?, ?, ?)""", (u[0], c[0], amount))
        await db.execute(
            """INSERT INTO currency_txns (user_id, currency_id, delta, reason, created_at)
               SELECT ?, ?, ?, 'demo seed', ?
               WHERE NOT EXISTS (SELECT 1 FROM currency_txns
                                 WHERE user_id = ? AND currency_id = ?
                                 AND reason = 'demo seed')""",
            (u[0], c[0], amount, now, u[0], c[0]))
    # Brad demos the gold frame
    cur = await db.execute("SELECT id FROM users WHERE username = 'krzy_budz'")
    u = await cur.fetchone()
    cur = await db.execute("SELECT id FROM shop_items WHERE name = 'Golden Leaf Frame'")
    it = await cur.fetchone()
    if u and it:
        await db.execute(
            "INSERT OR IGNORE INTO inventory (user_id, item_id, created_at) VALUES (?, ?, ?)",
            (u[0], it[0], now))
        await db.execute("UPDATE users SET equipped_frame = 'frame-gold' WHERE id = ?",
                         (u[0],))
    await db.commit()


async def main() -> None:
    os.makedirs(AVATAR_DIR, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await _migrate(db)  # column additions for pre-currency databases
        await db.commit()
        await seed_currencies(db)  # idempotent: safe on re-run
        cur = await db.execute("SELECT id FROM users WHERE username = 'admin'")
        if await cur.fetchone():
            print("Seed: admin exists, nothing to do.")
            return
        now = datetime.now(timezone.utc)
        ids = {}
        for i, (uname, pw, disp, twitch, bio, admin, approved) in enumerate(USERS):
            await db.execute(
                """INSERT INTO users (username, display_name, password_hash, bio,
                                      twitch_username, avatar_approved, is_admin, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (uname, disp, hash_password(pw), bio, twitch, int(approved),
                 int(admin), (now - timedelta(days=30)).isoformat()))
            cur = await db.execute("SELECT id FROM users WHERE username = ?", (uname,))
            ids[uname] = (await cur.fetchone())[0]
            if uname != "admin":
                make_avatar(uname, AVATAR_COLORS[i % len(AVATAR_COLORS)], disp[0])
        await db.commit()

        post_ids = []
        for uname, body, kind, mins_ago in POSTS:
            cur = await db.execute(
                """INSERT INTO posts (user_id, body, kind, created_at)
                   VALUES (?, ?, ?, ?)""",
                (ids[uname], body, kind, (now - timedelta(minutes=mins_ago)).isoformat()))
            post_ids.append(cur.lastrowid)
        # likes + comments
        await db.execute("INSERT INTO likes (user_id, post_id, created_at) VALUES (?, ?, ?)",
                         (ids["queenshida"], post_ids[0], now.isoformat()))
        await db.execute("INSERT INTO likes (user_id, post_id, created_at) VALUES (?, ?, ?)",
                         (ids["powme0wpow"], post_ids[0], now.isoformat()))
        await db.execute("INSERT INTO likes (user_id, post_id, created_at) VALUES (?, ?, ?)",
                         (ids["krzy_budz"], post_ids[2], now.isoformat()))
        await db.execute(
            "INSERT INTO comments (post_id, user_id, body, created_at) VALUES (?, ?, ?, ?)",
            (post_ids[0], ids["queenshida"], "Let's gooo! 🌿", now.isoformat()))
        await db.execute(
            "INSERT INTO comments (post_id, user_id, body, created_at) VALUES (?, ?, ?, ?)",
            (post_ids[2], ids["krzy_budz"], "Told you. Budz Book never misses.", now.isoformat()))
        for follower, followed in FOLLOWS:
            await db.execute(
                "INSERT INTO follows (follower_id, followed_id, created_at) VALUES (?, ?, ?)",
                (ids[follower], ids[followed], now.isoformat()))
        # DM thread brad <-> shida
        dms = [(ids["krzy_budz"], ids["queenshida"], "you coming through tonight?"),
               (ids["queenshida"], ids["krzy_budz"], "wouldn't miss it 👑"),
               (ids["krzy_budz"], ids["queenshida"], "say less. going live at 8")]
        for i, (s, r, body) in enumerate(dms):
            await db.execute(
                "INSERT INTO messages (sender_id, recipient_id, body, created_at)"
                " VALUES (?, ?, ?, ?)",
                (s, r, body, (now - timedelta(minutes=60 - i * 5)).isoformat()))
        # one open report so the admin view isn't empty
        await db.execute(
            "INSERT INTO reports (post_id, reporter_id, reason, created_at) VALUES (?, ?, ?, ?)",
            (post_ids[3], ids["powme0wpow"], "demo report — testing the mod queue",
             now.isoformat()))
        await db.commit()
        print(f"Seed: {len(USERS)} users, {len(POSTS)} posts, demo content ready.")
        print("Admin login: admin / admin123")


if __name__ == "__main__":
    asyncio.run(main())
