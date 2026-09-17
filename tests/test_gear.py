"""Tests for the avatar gear shop (hats/jerseys buyable with Budz).

Covers: the gear migration (slot/rpm_asset_id/equipped columns + seed items),
buying gear with BUDZ (success, insufficient funds, already-owned), auto-equip
on purchase, one-item-per-slot exclusivity, and equip toggle (unequip).
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="bb_gear_test_")
os.environ["THC_SOCIAL_DB"] = os.path.join(_tmp, "test.db")
os.environ["THC_SOCIAL_MEDIA"] = os.path.join(_tmp, "media")
os.makedirs(os.path.join(_tmp, "media", "avatars"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "posts"), exist_ok=True)

import aiosqlite  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import currency  # noqa: E402
from app.auth import AGE_COOKIE, make_age_token  # noqa: E402
from app.db import DB_PATH, init_db  # noqa: E402
from app.main import app  # noqa: E402

asyncio.run(init_db())


asyncio.run(init_db())

RESULTS = []


def check(name, cond):
    RESULTS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + name)


def _client():
    c = TestClient(app)
    c.cookies.set(AGE_COOKIE, make_age_token())
    return c


def _register(c, username, password="secret123"):
    return c.post("/register", data={
        "username": username, "display_name": username.title(),
        "password": password, "twitch_username": username},
        follow_redirects=False)


async def _db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def _user_id(username):
    db = await _db()
    cur = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
    uid = (await cur.fetchone())["id"]
    await db.close()
    return uid


async def _gear_id(name):
    db = await _db()
    cur = await db.execute(
        "SELECT id FROM shop_items WHERE name = ? AND kind = 'gear'", (name,))
    row = await cur.fetchone()
    await db.close()
    return row["id"] if row else None


async def _give_budz(username, amount):
    db = await _db()
    uid = await _user_id(username)
    await currency.award(db, uid, "BUDZ", amount, "test grant")
    await db.close()


# --- migration: columns + seed ---------------------------------------------
async def _cols(table):
    db = await _db()
    cur = await db.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in await cur.fetchall()]
    await db.close()
    return cols


check("shop_items has slot column", "slot" in asyncio.run(_cols("shop_items")))
check("shop_items has rpm_asset_id column",
      "rpm_asset_id" in asyncio.run(_cols("shop_items")))
check("inventory has equipped column", "equipped" in asyncio.run(_cols("inventory")))
check("Giants fitted seeded", asyncio.run(_gear_id("NY Giants 59FIFTY Fitted")) is not None)
check("Irvin jersey seeded",
      asyncio.run(_gear_id("Michael Irvin #88 Jersey")) is not None)

# --- buying -----------------------------------------------------------------
c = _client()
_register(c, "gearbuyer")
asyncio.run(_give_budz("gearbuyer", 1000))
uid = asyncio.run(_user_id("gearbuyer"))
hat = asyncio.run(_gear_id("NY Giants 59FIFTY Fitted"))
jersey = asyncio.run(_gear_id("Michael Irvin #88 Jersey"))
tee = asyncio.run(_gear_id("BudzBook Leaf Tee"))

r = c.post(f"/shop/buy/{hat}", follow_redirects=False)
check("buy hat redirects", r.status_code == 303)


async def _owns(uid_, iid):
    db = await _db()
    owns = await currency.owns_item(db, uid_, iid)
    await db.close()
    return owns


async def _worn(uid_):
    db = await _db()
    worn = await currency.equipped_gear(db, uid_)
    await db.close()
    return [g["name"] for g in worn]


check("hat owned after buy", asyncio.run(_owns(uid, hat)))
check("hat auto-equipped on buy",
      "NY Giants 59FIFTY Fitted" in asyncio.run(_worn(uid)))

r = c.post(f"/shop/buy/{hat}", follow_redirects=False)
check("re-buy redirect (already owned handled)", r.status_code == 303)
check("still owns hat once", asyncio.run(_owns(uid, hat)))

# --- slot exclusivity: second hat swaps the first off ------------------------
asyncio.run(_give_budz("gearbuyer", 1000))
cowboys = asyncio.run(_gear_id("Dallas Cowboys Cap"))
c.post(f"/shop/buy/{cowboys}", follow_redirects=False)
worn = asyncio.run(_worn(uid))
check("cowboys cap equipped", "Dallas Cowboys Cap" in worn)
check("giants hat auto-unequipped (one hat slot)",
      "NY Giants 59FIFTY Fitted" not in worn)

# --- jersey is a separate slot ----------------------------------------------
c.post(f"/shop/buy/{jersey}", follow_redirects=False)
worn = asyncio.run(_worn(uid))
check("jersey equipped alongside hat", "Michael Irvin #88 Jersey" in worn)
check("hat still worn", "Dallas Cowboys Cap" in worn)

# --- equip toggle (unequip) via the equip endpoint ---------------------------
r = c.post(f"/inventory/equip/{jersey}", follow_redirects=False)
check("toggle equip redirects", r.status_code == 303)
check("jersey unequipped after toggle",
      "Michael Irvin #88 Jersey" not in asyncio.run(_worn(uid)))
r = c.post(f"/inventory/equip/{jersey}", follow_redirects=False)
check("jersey re-equipped after second toggle",
      "Michael Irvin #88 Jersey" in asyncio.run(_worn(uid)))

# --- insufficient funds -------------------------------------------------------
c2 = _client()
_register(c2, "brokebuyer")
r = c2.post(f"/shop/buy/{tee}", follow_redirects=False)
check("broke buy redirects", r.status_code == 303)
uid2 = asyncio.run(_user_id("brokebuyer"))
check("broke buyer owns nothing", not asyncio.run(_owns(uid2, tee)))

# --- profile shows worn gear --------------------------------------------------
r = c.get("/u/gearbuyer")
check("profile shows equipped hat", "Dallas Cowboys Cap" in r.text)
check("profile shows equipped jersey", "Michael Irvin #88 Jersey" in r.text)

# --- shop page lists gear ------------------------------------------------------
r = c.get("/shop")
check("shop lists fitted", "NY Giants 59FIFTY Fitted" in r.text)
check("shop lists jersey", "Michael Irvin #88 Jersey" in r.text)

failed = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} gear checks passed")
if __name__ == "__main__":
    sys.exit(1 if failed else 0)
