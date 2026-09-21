"""Weekly franchise-export nudge.

Sends a BudzBook DM (from the @jarvis system user) to every user whose linked
Madden franchise data is stale (> STALE_DAYS since their last export push),
reminding them to push a fresh export from the Madden companion app.

Run weekly via the FranchiseNudgeWeekly Scheduled Task. Pattern mirrors
budz_picks.py: stdlib sqlite3 straight into thc_social.db; dedupe via the
meta table so nobody is nudged more than once per NUDGE_COOLDOWN_DAYS.
"""
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
EXPORT_DIR = os.path.join(BASE, "franchise_exports")
TOKENS_FILE = os.path.join(BASE, "franchise_user_tokens.json")
DB_PATH = os.path.join(BASE, "thc_social.db")
DEFAULT_LEAGUE = "1974224"
DEFAULT_OWNER_USERNAME = "Krzybudz"
STALE_DAYS = 7
NUDGE_COOLDOWN_DAYS = 7


def log(*a):
    print(*a, flush=True)


def parse_ts(ts):
    try:
        return datetime.strptime(str(ts), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def age_days(ts):
    dt = parse_ts(ts)
    if not dt:
        return None
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 86400))


def age_label(ts):
    d = age_days(ts)
    if d is None:
        return "a while"
    if d == 0:
        return "under a day"
    return "%d day%s" % (d, "" if d == 1 else "s")


def league_id_from_subpath(subpath):
    sub = "/" + (subpath or "")
    m = re.search(r"/(?:ps5|ps4|xbsx|xboxone|pc)/(\d+)/", sub)
    if m:
        return m.group(1)
    m = re.search(r"(\d{5,})", subpath or "")
    return m.group(1) if m else ""


def main():
    # --- scan export metas: per-user latest export, per-league latest export
    user_latest = {}
    league_latest = {}
    for m in glob.glob(os.path.join(EXPORT_DIR, "*.meta.json")):
        try:
            meta = json.load(open(m, encoding="utf-8"))
        except Exception:
            continue
        bin_path = m[: -len(".meta.json")] + ".bin"
        if not os.path.exists(bin_path):
            continue
        ts = meta.get("received_at") or os.path.basename(m).split("_")[0]
        lid = meta.get("league_id") or league_id_from_subpath(meta.get("subpath", ""))
        if lid:
            if ts > league_latest.get(lid, ""):
                league_latest[lid] = ts
        owner = meta.get("owner_user_id")
        if owner is not None:
            key = str(owner)
            if ts > user_latest.get(key, ""):
                user_latest[key] = ts

    if not os.path.exists(DB_PATH):
        log("nudge skipped: no db at", DB_PATH)
        return 0

    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    jrow = cur.execute("SELECT id FROM users WHERE username = 'jarvis' LIMIT 1").fetchone()
    if not jrow:
        log("nudge skipped: no jarvis user")
        return 0
    jarvis_id = jrow["id"]

    brow = cur.execute(
        "SELECT id FROM users WHERE username = ? LIMIT 1", (DEFAULT_OWNER_USERNAME,)
    ).fetchone()
    if brow and str(brow["id"]) not in user_latest and league_latest.get(DEFAULT_LEAGUE):
        # Brad's league arrives through the global token (no owner); its
        # latest export counts as his own update.
        user_latest[str(brow["id"])] = league_latest[DEFAULT_LEAGUE]

    sent = 0
    for uid, latest in user_latest.items():
        days = age_days(latest)
        if days is None or days <= STALE_DAYS:
            continue
        urow = cur.execute(
            "SELECT id, username FROM users WHERE id = ?", (uid,)
        ).fetchone()
        if not urow:
            continue
        mkey = "franchise_nudge_%s" % uid
        mrow = cur.execute(
            "SELECT value FROM meta WHERE key = ?", (mkey,)
        ).fetchone()
        if mrow:
            try:
                last = datetime.fromisoformat(mrow["value"])
                if (datetime.now(timezone.utc) - last).days < NUDGE_COOLDOWN_DAYS:
                    continue
            except Exception:
                pass
        body = (
            "Heads up: your Madden franchise data on BudzBook is %s old. "
            "Open the Madden companion app and push your export again -- "
            "one tap, same link, no re-linking: "
            "https://budzbook.us/franchise/link" % age_label(latest)
        )
        dup = cur.execute(
            "SELECT 1 FROM messages WHERE sender_id = ? AND recipient_id = ? "
            "AND body = ? LIMIT 1",
            (jarvis_id, urow["id"], body),
        ).fetchone()
        if dup:
            continue
        now = datetime.now(timezone.utc).isoformat()
        cur.execute(
            "INSERT INTO messages (sender_id, recipient_id, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (jarvis_id, urow["id"], body, now),
        )
        cur.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (mkey, now)
        )
        sent += 1
        log("nudged @%s (data %s old)" % (urow["username"], age_label(latest)))

    con.commit()
    con.close()
    log("franchise nudge done: %d sent" % sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
