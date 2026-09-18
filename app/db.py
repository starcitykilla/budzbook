"""SQLite database layer for BudzBook.

Uses aiosqlite (async wrapper around sqlite3). One connection per request,
created by the ``get_db`` FastAPI dependency and closed afterwards.

The database file lives at the project root (``thc_social.db``) unless the
``THC_SOCIAL_DB`` environment variable points somewhere else (the test suite
uses this to get an isolated throwaway database).
"""
import os
from datetime import datetime, timezone

import aiosqlite

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("THC_SOCIAL_DB", os.path.join(BASE_DIR, "thc_social.db"))

# User-uploaded media. Avatars live at media/avatars/<username>.jpg
# (square-cropped, max 512px). Post images live under media/posts/.
# Comment attachments live under media/comments/.
MEDIA_DIR = os.environ.get("THC_SOCIAL_MEDIA", os.path.join(BASE_DIR, "media"))
AVATAR_DIR = os.path.join(MEDIA_DIR, "avatars")
FIGURINE_DIR = os.path.join(MEDIA_DIR, "figurines")
POST_IMG_DIR = os.path.join(MEDIA_DIR, "posts")
COMMENT_IMG_DIR = os.path.join(MEDIA_DIR, "comments")
BANNER_DIR = os.path.join(MEDIA_DIR, "banners")
os.makedirs(COMMENT_IMG_DIR, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL COLLATE NOCASE,
    display_name    TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    bio             TEXT NOT NULL DEFAULT '',
    twitch_username TEXT NOT NULL DEFAULT '',
    -- 0 = no avatar or not yet approved, 1 = avatar approved for stream use
    avatar_approved INTEGER NOT NULL DEFAULT 0,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body       TEXT NOT NULL,
    image_path TEXT,                       -- e.g. "posts/ab12cd34.jpg" (relative to media/)
    kind       TEXT NOT NULL DEFAULT 'post', -- 'post' or 'grow' (grow-journal entry)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC);

CREATE TABLE IF NOT EXISTS likes (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, post_id)
);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS follows (
    follower_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    followed_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (follower_id, followed_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_pair ON messages(sender_id, recipient_id, id);

CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    reporter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reason      TEXT NOT NULL DEFAULT '',
    resolved    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- Game currencies (closed-loop: earned by watching/betting, no cash value,
-- no cash-out — like arcade tokens / channel points, per streamer).
CREATE TABLE IF NOT EXISTS currencies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT UNIQUE NOT NULL,   -- 'BUDZ'
    name         TEXT NOT NULL,          -- 'Budz'
    owner        TEXT NOT NULL DEFAULT '', -- streamer username (display only)
    icon         TEXT NOT NULL DEFAULT '🪙',
    rate_to_base REAL NOT NULL DEFAULT 1.0, -- fixed exchange rate vs base currency
    is_base      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wallets (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    currency_id INTEGER NOT NULL REFERENCES currencies(id) ON DELETE CASCADE,
    balance     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, currency_id)
);

-- Every balance change is logged (earn, claim, swap, fee, purchase, grant).
CREATE TABLE IF NOT EXISTS currency_txns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    currency_id INTEGER NOT NULL REFERENCES currencies(id) ON DELETE CASCADE,
    delta       INTEGER NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_txns_user ON currency_txns(user_id, id DESC);

-- Shop: collectible frames/badges bought with game currency.
CREATE TABLE IF NOT EXISTS shop_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,              -- 'frame' (avatar ring), 'badge', or 'gear' (hat/jersey)
    css_class   TEXT NOT NULL DEFAULT '',  -- frame class applied to avatars
    price       INTEGER NOT NULL,
    currency_id INTEGER NOT NULL REFERENCES currencies(id) ON DELETE CASCADE,
    icon        TEXT NOT NULL DEFAULT '🎁',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id    INTEGER NOT NULL REFERENCES shop_items(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, item_id)
);

-- Daily faucet claims (one per user per currency per day).
CREATE TABLE IF NOT EXISTS claims (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    currency_id INTEGER NOT NULL REFERENCES currencies(id) ON DELETE CASCADE,
    claimed_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, currency_id)
);
"""


async def _migrate(db) -> None:
    """Column additions for databases created before the currency update."""
    cur = await db.execute("PRAGMA table_info(users)")
    cols = [r[1] for r in await cur.fetchall()]  # plain tuples here (no row_factory)
    if "equipped_frame" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN equipped_frame TEXT NOT NULL DEFAULT ''")
    # Twitch OAuth link columns (verified identity for wallet payouts).
    if "twitch_id" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN twitch_id TEXT NOT NULL DEFAULT ''")
    if "twitch_avatar" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN twitch_avatar TEXT NOT NULL DEFAULT ''")
    if "twitch_verified" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN twitch_verified INTEGER NOT NULL DEFAULT 0")
    # Ready Player Me 3D avatar (GLB URL exported from the avatar creator).
    if "avatar_3d_url" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN avatar_3d_url TEXT NOT NULL DEFAULT ''")
    # BMG employee badge (Blindman Gaming staff pill on profiles).
    if "badge" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN badge TEXT NOT NULL DEFAULT ''")
    # In-house avatar builder: saved PNG filename + part config JSON.
    if "avatar_builder_png" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN avatar_builder_png TEXT NOT NULL DEFAULT ''")
    if "avatar_builder_config" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN avatar_builder_config TEXT NOT NULL DEFAULT ''")
    # Terms of Service acceptance (clickwrap): UTC ISO timestamp of
    # acceptance + the TERMS_VERSION that was accepted. NULL means the
    # user has not accepted and is gated to /terms by middleware.
    if "terms_accepted_at" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN terms_accepted_at TEXT")
    if "terms_version" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN terms_version TEXT")
    # Avatar gear shop: hats/jerseys buyable with Budz (chat + site).
    cur = await db.execute("PRAGMA table_info(shop_items)")
    scols = [r[1] for r in await cur.fetchall()]
    if "slot" not in scols:
        await db.execute("ALTER TABLE shop_items ADD COLUMN slot TEXT NOT NULL DEFAULT ''")
    if "rpm_asset_id" not in scols:
        await db.execute(
            "ALTER TABLE shop_items ADD COLUMN rpm_asset_id TEXT NOT NULL DEFAULT ''")
    cur = await db.execute("PRAGMA table_info(inventory)")
    icols = [r[1] for r in await cur.fetchall()]
    if "equipped" not in icols:
        await db.execute(
            "ALTER TABLE inventory ADD COLUMN equipped INTEGER NOT NULL DEFAULT 0")
    # Seed the base BUDZ currency (idempotent) — fresh DBs need it before
    # the gear shelf below, and the welcome bonus / faucet need it too.
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT OR IGNORE INTO currencies"
        " (code, name, owner, icon, rate_to_base, is_base, created_at)"
        " VALUES ('BUDZ', 'Budz', 'krzy_budz', ?, 1.0, 1, ?)",
        ("\U0001f33f", now))
    # Seed the gear shelf (base currency) — idempotent by name.
    cur = await db.execute("SELECT id FROM currencies WHERE is_base = 1 LIMIT 1")
    base = await cur.fetchone()
    if base:
        base_id = base[0]  # plain tuple here (no row_factory in _migrate)
        now = datetime.now(timezone.utc).isoformat()
        for name, slot, price, icon in [
            ("New York Giants Fitted", "hat", 250, "🧢"),
            ("Dallas Cowboys Cap", "hat", 250, "🧢"),
            ("Michael Irvin #88 Jersey", "jersey", 500, "👕"),
            ("BudzBook Leaf Tee", "jersey", 100, "👕"),
        ]:
            cur = await db.execute("SELECT id FROM shop_items WHERE name = ?", (name,))
            if not await cur.fetchone():
                await db.execute(
                    "INSERT INTO shop_items (name, kind, slot, css_class, price,"
                    " currency_id, icon, created_at)"
                    " VALUES (?, 'gear', ?, '', ?, ?, ?, ?)",
                    (name, slot, price, base_id, icon, now))
    await _migrate_social(db)


async def _migrate_social(db) -> None:
    """Hashtags, groups, go-live dedupe, video posts (v2 social features)."""
    await db.execute("""CREATE TABLE IF NOT EXISTS post_hashtags (
        post_id INTEGER NOT NULL, tag TEXT NOT NULL,
        PRIMARY KEY (post_id, tag))""")
    await db.execute("""CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '', owner_id INTEGER NOT NULL,
        created_at TEXT NOT NULL)""")
    await db.execute("""CREATE TABLE IF NOT EXISTS group_members (
        group_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        joined_at TEXT NOT NULL, PRIMARY KEY (group_id, user_id))""")
    await db.execute("""CREATE TABLE IF NOT EXISTS golive_posts (
        stream_id TEXT PRIMARY KEY, post_id INTEGER NOT NULL,
        created_at TEXT NOT NULL)""")
    await db.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    for table, coldef in (("posts", "group_id INTEGER"),
                          ("posts", "video_path TEXT"),
                          ("comments", "gif_url TEXT NOT NULL DEFAULT ''"),
                          ("comments", "image_path TEXT"),
                          ("comments", "video_path TEXT"),
                          ("users", "theme_color TEXT NOT NULL DEFAULT ''"),
                          ("users", "banner_path TEXT NOT NULL DEFAULT ''"),
                          ("users", "feed_prefs TEXT NOT NULL DEFAULT ''")):
        cur = await db.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in await cur.fetchall()]
        col = coldef.split()[0]
        if col not in cols:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")


async def init_db() -> None:
    """Create media dirs and all tables (safe to run on every startup)."""
    os.makedirs(AVATAR_DIR, exist_ok=True)
    os.makedirs(FIGURINE_DIR, exist_ok=True)
    os.makedirs(POST_IMG_DIR, exist_ok=True)
    os.makedirs(BANNER_DIR, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await _migrate(db)
        await db.commit()


async def get_db():
    """FastAPI dependency: yields one request-scoped connection."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


def avatar_path_for(username: str) -> str:
    """Absolute filesystem path of a user's avatar file."""
    return os.path.join(AVATAR_DIR, f"{username}.jpg")


def avatar_exists(username: str) -> bool:
    return os.path.isfile(avatar_path_for(username))


def figurine_exists(username: str) -> bool:
    """True when a spinnable figurine asset pair exists for the user."""
    return os.path.isfile(os.path.join(FIGURINE_DIR, f"{username}.png")) and \
        os.path.isfile(os.path.join(FIGURINE_DIR, f"{username}.json"))


def builder_avatar_path_for(username: str) -> str:
    """Absolute filesystem path of a user's in-house builder avatar PNG."""
    return os.path.join(AVATAR_DIR, f"{username}_builder.png")
