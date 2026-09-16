"""Game-currency engine for BudzBook: per-streamer currencies, wallets,
exchange, daily faucet, and the collectibles shop.

Closed-loop by design: currencies are earned (watching, betting, daily
claims) and spent (shop, swaps) inside the community. They have no cash
value and cannot be cashed out — arcade tokens, not money.

Exchange uses fixed rates vs the base currency (Budz) with a small house
fee. Amounts are integers; the swap floors in the house's favor.
"""
from datetime import datetime, timezone

EXCHANGE_FEE = 0.02          # 2% house fee on swaps, burned
DAILY_CLAIM_AMOUNT = 50      # faucet payout in base currency per day


def _now():
    return datetime.now(timezone.utc).isoformat()


async def get_currencies(db):
    cur = await db.execute("SELECT * FROM currencies ORDER BY is_base DESC, code")
    return [dict(r) for r in await cur.fetchall()]


async def get_currency(db, code):
    cur = await db.execute("SELECT * FROM currencies WHERE code = ?", (code.upper(),))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_base_currency(db):
    cur = await db.execute("SELECT * FROM currencies WHERE is_base = 1 LIMIT 1")
    row = await cur.fetchone()
    return dict(row) if row else None


async def wallet_balance(db, user_id, currency_id) -> int:
    cur = await db.execute(
        "SELECT balance FROM wallets WHERE user_id = ? AND currency_id = ?",
        (user_id, currency_id))
    row = await cur.fetchone()
    return row["balance"] if row else 0


async def wallets_for(db, user_id):
    """All of a user's wallets with currency info, richest first."""
    cur = await db.execute(
        """SELECT c.*, COALESCE(w.balance, 0) AS balance
           FROM currencies c LEFT JOIN wallets w
             ON w.currency_id = c.id AND w.user_id = ?
           ORDER BY balance DESC, c.code""", (user_id,))
    return [dict(r) for r in await cur.fetchall()]


async def _log(db, user_id, currency_id, delta, reason):
    await db.execute(
        "INSERT INTO currency_txns (user_id, currency_id, delta, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?)", (user_id, currency_id, delta, reason, _now()))


async def award(db, user_id, code, amount, reason):
    """Credit currency. Bot hook: the Twitch bot calls this for watch-time
    and betting payouts (see thc_adapter.award_currency)."""
    if amount <= 0:
        raise ValueError("award amount must be positive")
    cur = await get_currency(db, code)
    if not cur:
        raise ValueError(f"unknown currency {code}")
    await db.execute(
        """INSERT INTO wallets (user_id, currency_id, balance) VALUES (?, ?, ?)
           ON CONFLICT(user_id, currency_id)
           DO UPDATE SET balance = balance + ?""",
        (user_id, cur["id"], amount, amount))
    await _log(db, user_id, cur["id"], amount, reason)
    await db.commit()
    return await wallet_balance(db, user_id, cur["id"])


async def spend(db, user_id, code, amount, reason) -> bool:
    """Debit currency. Returns False when the balance is insufficient."""
    if amount <= 0:
        raise ValueError("spend amount must be positive")
    cur = await get_currency(db, code)
    if not cur:
        raise ValueError(f"unknown currency {code}")
    bal = await wallet_balance(db, user_id, cur["id"])
    if bal < amount:
        return False
    await db.execute(
        "UPDATE wallets SET balance = balance - ? WHERE user_id = ? AND currency_id = ?",
        (amount, user_id, cur["id"]))
    await _log(db, user_id, cur["id"], -amount, reason)
    await db.commit()
    return True


async def quote_swap(from_cur, to_cur, amount_in) -> int:
    """How much of to_cur you'd get for amount_in of from_cur (fee included)."""
    if amount_in <= 0:
        return 0
    base_value = amount_in * from_cur["rate_to_base"]
    return int(base_value * (1 - EXCHANGE_FEE) / to_cur["rate_to_base"])


async def swap(db, user_id, from_code, to_code, amount_in):
    """Exchange one streamer currency for another. Returns (ok, msg, amount_out)."""
    from_code, to_code = from_code.upper(), to_code.upper()
    if from_code == to_code:
        return False, "Pick two different currencies.", 0
    if amount_in <= 0:
        return False, "Amount must be positive.", 0
    fcur, tcur = await get_currency(db, from_code), await get_currency(db, to_code)
    if not fcur or not tcur:
        return False, "Unknown currency.", 0
    amount_out = await quote_swap(fcur, tcur, amount_in)
    if amount_out < 1:
        return False, "That little doesn't survive the exchange fee — swap more.", 0
    if not await spend(db, user_id, from_code, amount_in,
                       f"swap {from_code}->{to_code} (2% fee)"):
        return False, f"Not enough {fcur['name']} — you need {amount_in}.", 0
    await award(db, user_id, to_code, amount_out, f"swap {from_code}->{to_code} (2% fee)")
    return True, f"Swapped {amount_in} {fcur['icon']}{from_code} → {amount_out} {tcur['icon']}{to_code}.", amount_out


async def claim_daily(db, user_id):
    """Daily faucet: free base currency once per 24h."""
    base = await get_base_currency(db)
    if not base:
        return False, "No base currency yet."
    cur = await db.execute(
        "SELECT claimed_at FROM claims WHERE user_id = ? AND currency_id = ?",
        (user_id, base["id"]))
    row = await cur.fetchone()
    if row:
        last = datetime.fromisoformat(row["claimed_at"])
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        if elapsed < 24 * 3600:
            hrs = int((24 * 3600 - elapsed) // 3600) + 1
            return False, f"Faucet's dry — come back in ~{hrs}h."
    await award(db, user_id, base["code"], DAILY_CLAIM_AMOUNT, "daily faucet claim")
    await db.execute(
        """INSERT INTO claims (user_id, currency_id, claimed_at) VALUES (?, ?, ?)
           ON CONFLICT(user_id, currency_id) DO UPDATE SET claimed_at = ?""",
        (user_id, base["id"], _now(), _now()))
    await db.commit()
    return True, f"Claimed {DAILY_CLAIM_AMOUNT} {base['icon']}{base['code']} — see you tomorrow."


async def get_shop_items(db):
    cur = await db.execute(
        """SELECT s.*, c.code AS currency_code, c.icon AS currency_icon
           FROM shop_items s JOIN currencies c ON c.id = s.currency_id
           ORDER BY s.price""")
    return [dict(r) for r in await cur.fetchall()]


async def inventory_for(db, user_id):
    cur = await db.execute(
        """SELECT s.*, i.equipped FROM inventory i JOIN shop_items s ON s.id = i.item_id
           WHERE i.user_id = ? ORDER BY i.created_at DESC""", (user_id,))
    return [dict(r) for r in await cur.fetchall()]


async def equipped_gear(db, user_id):
    """Gear currently worn (one per slot: hat, jersey). Shown on profiles."""
    cur = await db.execute(
        """SELECT s.* FROM inventory i JOIN shop_items s ON s.id = i.item_id
           WHERE i.user_id = ? AND i.equipped = 1 AND s.kind = 'gear'""", (user_id,))
    return [dict(r) for r in await cur.fetchall()]


async def _equip_gear(db, user_id, item_id, slot):
    """Wear gear: one item per slot, so a new hat swaps the old one off."""
    if slot:
        await db.execute(
            """UPDATE inventory SET equipped = 0 WHERE user_id = ?
               AND item_id IN (SELECT id FROM shop_items
                               WHERE kind = 'gear' AND slot = ?)""",
            (user_id, slot))
    await db.execute(
        "UPDATE inventory SET equipped = 1 WHERE user_id = ? AND item_id = ?",
        (user_id, item_id))


async def owns_item(db, user_id, item_id) -> bool:
    cur = await db.execute(
        "SELECT 1 FROM inventory WHERE user_id = ? AND item_id = ?",
        (user_id, item_id))
    return bool(await cur.fetchone())


async def buy_item(db, user_id, item_id):
    """Buy a shop item. Frames and gear auto-equip on purchase."""
    cur = await db.execute("SELECT * FROM shop_items WHERE id = ?", (item_id,))
    item = await cur.fetchone()
    if not item:
        return False, "That item doesn't exist."
    item = dict(item)
    if await owns_item(db, user_id, item_id):
        return False, "You already own that."
    cur = await db.execute("SELECT code FROM currencies WHERE id = ?", (item["currency_id"],))
    code = (await cur.fetchone())["code"]
    if not await spend(db, user_id, code, item["price"], f"shop: {item['name']}"):
        return False, f"Not enough {code} — it costs {item['price']}."
    await db.execute(
        "INSERT INTO inventory (user_id, item_id, created_at) VALUES (?, ?, ?)",
        (user_id, item_id, _now()))
    extra = ""
    if item["kind"] == "frame" and item["css_class"]:
        await db.execute("UPDATE users SET equipped_frame = ? WHERE id = ?",
                         (item["css_class"], user_id))
        extra = " It's equipped — check your avatar."
    elif item["kind"] == "gear":
        await _equip_gear(db, user_id, item_id, item.get("slot", ""))
        extra = " It's equipped — check your profile."
    await db.commit()
    return True, f"{item['icon']} {item['name']} is yours.{extra}"


async def equip_item(db, user_id, item_id):
    """Equip an owned frame, toggle owned gear, or unequip frame with id 0."""
    if item_id == 0:
        await db.execute("UPDATE users SET equipped_frame = '' WHERE id = ?", (user_id,))
        await db.commit()
        return True, "Frame removed."
    if not await owns_item(db, user_id, item_id):
        return False, "You don't own that."
    cur = await db.execute(
        "SELECT kind, css_class, slot, name, icon FROM shop_items WHERE id = ?",
        (item_id,))
    item = await cur.fetchone()
    if not item:
        return False, "That item doesn't exist."
    item = dict(item)
    if item["kind"] == "frame":
        if not item["css_class"]:
            return False, "That frame can't be equipped."
        await db.execute("UPDATE users SET equipped_frame = ? WHERE id = ?",
                         (item["css_class"], user_id))
        await db.commit()
        return True, "Frame equipped."
    if item["kind"] == "gear":
        cur = await db.execute(
            "SELECT equipped FROM inventory WHERE user_id = ? AND item_id = ?",
            (user_id, item_id))
        if (await cur.fetchone())["equipped"]:
            await db.execute(
                "UPDATE inventory SET equipped = 0 WHERE user_id = ? AND item_id = ?",
                (user_id, item_id))
            await db.commit()
            return True, f"{item['icon']} {item['name']} unequipped."
        await _equip_gear(db, user_id, item_id, item.get("slot", ""))
        await db.commit()
        return True, f"{item['icon']} {item['name']} equipped."
    return False, "Only frames and gear can be equipped."


async def recent_txns(db, user_id, limit=15):
    cur = await db.execute(
        """SELECT t.*, c.code, c.icon FROM currency_txns t
           JOIN currencies c ON c.id = t.currency_id
           WHERE t.user_id = ? ORDER BY t.id DESC LIMIT ?""", (user_id, limit))
    return [dict(r) for r in await cur.fetchall()]
