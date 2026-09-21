#!/usr/bin/env python3
"""One-shot data pull for the cloud Twitch picks/sweat relay.

Called by the `twitch-picks-sweat` cron on Brad's PC via SSH:
    python.exe C:\\Users\\Starc\\thc-social\\pc_picks_sweat.py <since_comment_id>

Prints one JSON object to stdout:
  {"date_iso": <budz_picks.json sheet date>,
   "tickets": [{"name":..., "combined_odds":..., "legs":["Padres ML", ...]}],
   "post_id": <today's @jarvis Budz Picks post id or None>,
   "comments": [{"id":..., "body":...}]}

comments = @jarvis sweat-ticker comments on today's picks post with
id > since_comment_id, oldest first. Read-only: never writes the DB.
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

SOCIAL = r"C:\Users\Starc\thc-social"
SHEET_PATH = os.path.join(SOCIAL, "budz_picks.json")
DB_PATH = os.path.join(SOCIAL, "thc_social.db")


def nick(team):
    parts = (team or "").split()
    return parts[-1] if parts else team


def leg_short(leg):
    team = leg["home"] if leg.get("pick") == "home" else leg["away"]
    desc = leg.get("desc", "")
    tail = desc.split(" ML", 1)[1] if " ML" in desc else ""
    return "%s ML%s" % (nick(team), tail)


def main():
    since_id = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    out = {"date_iso": None, "tickets": [], "post_id": None, "comments": []}

    try:
        with open(SHEET_PATH, "r", encoding="utf-8") as f:
            sheet = json.load(f)
    except Exception as e:
        out["error"] = "no sheet: %s" % e
        print(json.dumps(out))
        return
    out["date_iso"] = sheet.get("date_iso")
    for t in sheet.get("tickets") or []:
        out["tickets"].append({
            "name": t.get("name", "?"),
            "combined_odds": t.get("combined_odds", ""),
            "legs": [leg_short(l) for l in t.get("legs", []) or []],
        })

    try:
        con = sqlite3.connect(DB_PATH, timeout=15)
    except Exception as e:
        out["error"] = "no db: %s" % e
        print(json.dumps(out))
        return
    try:
        row = con.execute(
            "SELECT id FROM users WHERE username = 'jarvis'").fetchone()
        if not row:
            print(json.dumps(out))
            return
        uid = row[0]
        prow = con.execute(
            "SELECT id, created_at FROM posts WHERE user_id = ?"
            " AND kind = 'post' AND body LIKE '%Budz Picks%'"
            " ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        if not prow:
            print(json.dumps(out))
            return
        try:
            cdt = datetime.fromisoformat(
                (prow[1] or "").replace("Z", "+00:00"))
        except Exception:
            cdt = None
        if cdt and (datetime.now(timezone.utc) - cdt).total_seconds() > 36 * 3600:
            print(json.dumps(out))  # stale post: no comments worth relaying
            return
        out["post_id"] = prow[0]
        for cid, body, cat in con.execute(
                "SELECT id, body, created_at FROM comments WHERE post_id = ?"
                " AND user_id = ? AND id > ? ORDER BY id ASC",
                (prow[0], uid, since_id)).fetchall():
            out["comments"].append(
                {"id": cid, "body": body or "", "created_at": cat or ""})
    finally:
        con.close()
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
