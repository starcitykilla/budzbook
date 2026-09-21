"""Budz Picks sweat ticker — live game updates as @jarvis comments.

Windows Scheduled Task `BudzSweatTicker` runs this every 15 minutes:
    C:\\Users\\Starc\\AppData\\Local\\Programs\\Python\\Python312\\python.exe
        C:\\Users\\Starc\\thc-social\\sweat_ticker.py

Reads today's sheet from budz_picks.json, tracks each leg against ESPN's
free scoreboard, and posts @jarvis comments on today's picks post ONLY on
meaningful transitions (ticker goes live, a leg goes live, a leg goes
final, a ticket is decided). No periodic score spam.

Also writes app/static/overlay/triggers/sweat.json every run for the
stream overlay: live:false + empty tickets when nothing is in play.

--dry-run prints what WOULD be posted without touching the DB.
"""
import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

SOCIAL = r'C:\Users\Starc\thc-social'
THC = r'C:\Users\Starc\thc-test'
sys.path.insert(0, THC)

try:
    import sportsbook
except ImportError:
    sportsbook = None

# EDT (UTC-4) — fine for the baseball/football season window.
ET = timezone(timedelta(hours=-4))

STATE_PATH = os.path.join(SOCIAL, 'sweat_state.json')
SHEET_PATH = os.path.join(SOCIAL, 'budz_picks.json')
OVERLAY_PATH = os.path.join(
    SOCIAL, 'app', 'static', 'overlay', 'triggers', 'sweat.json')

_ESPN_URLS = (
    "https://site.api.espn.com/apis/site/v2/sports/%s/scoreboard",
    "https://site.web.api.espn.com/apis/site/v2/sports/%s/scoreboard",
    "http://site.api.espn.com/apis/site/v2/sports/%s/scoreboard",
)


def _http_get_json(url, timeout=20):
    if sportsbook is not None:
        return sportsbook._http_get_json(url, timeout=timeout)
    req = urllib.request.Request(
        url, headers={"User-Agent": "THC-BudzBook/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _nick(team):
    parts = (team or "").split()
    return parts[-1] if parts else team


def _abbr(team, espn_abbr=None):
    if espn_abbr:
        return espn_abbr
    w = _nick(team)
    return (w[:3] if len(w) <= 4 else w[:3]).upper()


def fetch_board(sport_key):
    """ESPN scoreboard with per-game state, scores, abbrs, and detail text."""
    espn = sportsbook.SPORTS[sport_key]["espn"] if sportsbook else None
    if not espn:
        espn = {"nfl": "football/nfl", "mlb": "baseball/mlb",
               "nhl": "hockey/nhl", "nba": "basketball/nba"}[sport_key]
    last_err = Exception("no ESPN host reachable")
    for tmpl in _ESPN_URLS:
        try:
            data = _http_get_json(tmpl % espn)
            break
        except Exception as e:
            last_err = e
    else:
        raise last_err
    games = []
    for ev in data.get("events", []) or []:
        comp = (ev.get("competitions") or [{}])[0]
        st = (ev.get("status") or {}).get("type") or {}
        home = away = None
        for c in comp.get("competitors", []) or []:
            team = c.get("team") or {}
            try:
                score = int(c.get("score") or 0)
            except (TypeError, ValueError):
                score = 0
            info = {"name": team.get("displayName", ""),
                    "abbr": team.get("abbreviation", ""),
                    "score": score}
            if c.get("homeAway") == "home":
                home = info
            else:
                away = info
        games.append({
            "home": home["name"] if home else "",
            "away": away["name"] if away else "",
            "home_abbr": home["abbr"] if home else "",
            "away_abbr": away["abbr"] if away else "",
            "home_score": home["score"] if home else 0,
            "away_score": away["score"] if away else 0,
            "state": st.get("state", "pre"),       # pre | in | post
            "detail": st.get("shortDetail", ""),   # "Top 7th", "Q3 4:12", "Final"
            "date": ev.get("date", ""),
        })
    return games


def _utc_date(iso):
    try:
        return datetime.fromisoformat(
            (iso or "").replace("Z", "+00:00")).date()
    except Exception:
        return None


def find_game(board, home, away, commence=None):
    if sportsbook is not None:
        return sportsbook.find_espn_game(board, home, away, commence)
    nh, na = _norm(home), _norm(away)
    cands = [g for g in board
             if _norm(g["home"]) == nh and _norm(g["away"]) == na]
    if not cands:
        return None
    if commence:
        want = _utc_date(commence)
        dated = [g for g in cands if _utc_date(g.get("date")) is not None]
        if want is not None and dated:
            same = [g for g in dated if _utc_date(g.get("date")) == want]
            return same[0] if same else None
    return cands[0]


def settle(leg, game):
    if sportsbook is not None:
        return sportsbook.settle_leg(leg, game)
    hs, aws = game["home_score"], game["away_score"]
    mine = hs if leg["pick"] == "home" else aws
    theirs = aws if leg["pick"] == "home" else hs
    if mine > theirs:
        return "win"
    return "push" if mine == theirs else "loss"


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def fmt_et(dt):
    hr = dt.hour % 12 or 12
    return "%d:%02d %s ET" % (hr, dt.minute, dt.strftime("%p"))


def parse_dt(iso):
    try:
        return datetime.fromisoformat(
            (iso or "").replace("Z", "+00:00"))
    except Exception:
        return None


def ticket_label(name):
    n = (name or "").lower()
    if "safe" in n:
        return "SAFE"
    if "lotto" in n:
        return "LOTTO"
    return (name or "?").split()[0].upper()


def leg_short(leg):
    team = leg["home"] if leg["pick"] == "home" else leg["away"]
    desc = leg.get("desc", "")
    tail = desc.split(" ML", 1)[1] if " ML" in desc else ""
    return "%s ML%s" % (_nick(team), tail)


def main():
    dry = "--dry-run" in sys.argv
    today = datetime.now(ET).date().isoformat()

    try:
        with open(SHEET_PATH, "r", encoding="utf-8") as f:
            sheet = json.load(f)
    except Exception as e:
        print("no sheet (%s)" % e)
        write_overlay({"live": False, "updated_at": datetime.now(ET).isoformat(),
                       "tickets": []})
        return

    tickets = sheet.get("tickets") or []
    if sheet.get("date_iso") != today or not tickets:
        print("no legs for today (%s)" % sheet.get("date_iso"))
        write_overlay({"live": False, "updated_at": datetime.now(ET).isoformat(),
                       "tickets": []})
        return

    # --- gather legs ---
    legs = []  # (ticket_name, idx, leg)
    sports = set()
    for t in tickets:
        for i, leg in enumerate(t.get("legs", []) or []):
            legs.append((t["name"], i, leg))
            sports.add(leg["sport"])

    boards = {}
    for s in sports:
        try:
            boards[s] = fetch_board(s)
        except Exception as e:
            print("board fetch failed for %s: %s" % (s, e))
            boards[s] = []

    # --- per-leg status ---
    rows = []  # dicts with ticket, idx, leg, game, state, result
    for tname, i, leg in legs:
        game = find_game(boards.get(leg["sport"], []), leg["home"],
                        leg["away"], leg.get("commence"))
        state = game["state"] if game else "pre"
        result = None
        if state == "post":
            result = settle(leg, game)
        rows.append({"ticket": tname, "idx": i, "leg": leg, "game": game,
                     "state": state, "result": result})

    # --- state ---
    state = load_state()
    day = state.setdefault(today, {"post_id": None, "live_announced": False,
                                   "legs": {}, "tickets_final": {}})

    # --- today's picks post (latest @jarvis Budz Picks post) ---
    db_path = os.path.join(SOCIAL, "thc_social.db")
    con = sqlite3.connect(db_path, timeout=15)
    try:
        row = con.execute(
            "SELECT id FROM users WHERE username = 'jarvis'").fetchone()
        if not row:
            print("no jarvis user; overlay only")
            con.close()
            write_overlay(build_payload(rows, today))
            return
        uid = row[0]
        prow = con.execute(
            "SELECT id, created_at FROM posts WHERE user_id = ?"
            " AND kind = 'post' AND body LIKE '%Budz Picks%'"
            " ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        post_id = prow[0] if prow else None
        if prow:
            cdt = parse_dt(prow[1])
            if cdt and (datetime.now(timezone.utc) - cdt).total_seconds() > 36 * 3600:
                post_id = None  # stale post — don't comment on it
        if post_id:
            day["post_id"] = post_id
    finally:
        con.close()

    # --- transitions ---
    comments = []
    any_in = any(r["state"] == "in" for r in rows)

    if any_in and not day["live_announced"] and post_id:
        bits = []
        for t in tickets:
            shorts = ", ".join(leg_short(l) for l in t.get("legs", []) or [])
            bits.append("%s: %s" % (ticket_label(t["name"]), shorts))
        comments.append(
            "\U0001F534 Sweat ticker is LIVE — riding today's tickets with you.\n"
            + "\n".join(bits)
            + "\nUpdates as the games play out. Let's sweat \U0001F37F")
        day["live_announced"] = True

    for r in rows:
        key = "%s|%d" % (r["ticket"], r["idx"])
        ls = day["legs"].setdefault(key, {"started": False, "final": False})
        leg = r["leg"]
        team = leg["home"] if leg["pick"] == "home" else leg["away"]
        opp = leg["away"] if leg["pick"] == "home" else leg["home"]
        if r["state"] == "in" and not ls["started"] and post_id:
            ls["started"] = True
            if day["live_announced"] and not any(
                    c.startswith("\U0001F534") for c in comments):
                # live announce covers simultaneous starts; note late starters
                detail = (r["game"] or {}).get("detail", "")
                comments.append(
                    "▶️ %s underway — %s leg %d in play%s" % (
                        _nick(team), ticket_label(r["ticket"]).lower(),
                        r["idx"] + 1, (" (%s)" % detail) if detail else ""))
        if r["state"] == "post" and not ls["final"] and post_id:
            ls["final"] = True
            ls["result"] = r["result"]
            mine = r["game"]["home_score"] if leg["pick"] == "home" else r["game"]["away_score"]
            theirs = r["game"]["away_score"] if leg["pick"] == "home" else r["game"]["home_score"]
            if r["result"] == "win":
                comments.append(
                    "FINAL: %s %d, %s %d ✅ — %s leg %d hits" % (
                        _nick(team), mine, _nick(opp), theirs,
                        ticket_label(r["ticket"]).lower(), r["idx"] + 1))
            elif r["result"] == "push":
                comments.append(
                    "FINAL: %s %d, %s %d 🤝 — %s leg %d pushes" % (
                        _nick(team), mine, _nick(opp), theirs,
                        ticket_label(r["ticket"]).lower(), r["idx"] + 1))
            else:
                comments.append(
                    "FINAL: %s %d, %s %d ❌ — %s leg %d misses" % (
                        _nick(team), mine, _nick(opp), theirs,
                        ticket_label(r["ticket"]).lower(), r["idx"] + 1))

    # --- ticket summaries ---
    for t in tickets:
        tname = t["name"]
        if day["tickets_final"].get(tname):
            continue
        trows = [r for r in rows if r["ticket"] == tname]
        if not trows or not all(r["state"] == "post" for r in trows):
            continue
        day["tickets_final"][tname] = True
        if not post_id:
            continue
        wins = sum(1 for r in trows if r["result"] == "win")
        losses = sum(1 for r in trows if r["result"] == "loss")
        label = ticket_label(tname)
        if losses == 0 and wins > 0:
            comments.append(
                "%s ticket: %d-%d ✅ CASH IT \U0001F3C6" % (label, wins, losses))
        else:
            comments.append(
                "%s ticket: %d-%d ❌ busted" % (label, wins, losses))

    # --- overlay payload (every run) ---
    write_overlay(build_payload(rows, today))

    if dry:
        if comments:
            print("WOULD POST %d comment(s) on post %s:" % (len(comments), post_id))
            for c in comments:
                print("---")
                print(c)
        else:
            print("dry run: nothing to post")
        return

    if comments and post_id:
        con = sqlite3.connect(db_path, timeout=15)
        try:
            now = datetime.now(timezone.utc).isoformat()
            for c in comments:
                con.execute(
                    "INSERT INTO comments (post_id, user_id, body, created_at)"
                    " VALUES (?, ?, ?, ?)", (post_id, uid, c, now))
            con.commit()
            print("posted %d comment(s) on post %d" % (len(comments), post_id))
        finally:
            con.close()
    else:
        print("nothing to post")
    save_state(state)


def build_payload(rows, today):
    any_in = any(r["state"] == "in" for r in rows)
    payload = {"live": any_in, "updated_at": datetime.now(ET).isoformat(),
               "tickets": []}
    if not any_in:
        return payload
    seen = []
    for r in rows:
        if r["ticket"] not in seen:
            seen.append(r["ticket"])
    for tname in seen:
        trows = [r for r in rows if r["ticket"] == tname]
        status = "final" if all(r["state"] == "post" for r in trows) else "live"
        wins = sum(1 for r in trows if r["result"] == "win")
        losses = sum(1 for r in trows if r["result"] == "loss")
        tlegs = []
        for r in trows:
            leg = r["leg"]
            g = r["game"] or {}
            mine = g.get("home_score", 0) if leg["pick"] == "home" else g.get("away_score", 0)
            theirs = g.get("away_score", 0) if leg["pick"] == "home" else g.get("home_score", 0)
            team = leg["home"] if leg["pick"] == "home" else leg["away"]
            opp = leg["away"] if leg["pick"] == "home" else leg["home"]
            if r["state"] == "pre":
                dt = parse_dt(leg.get("commence") or g.get("date"))
                detail = fmt_et(dt.astimezone(ET)) if dt else ""
            else:
                detail = g.get("detail", "")
            result = None
            if r["result"] == "win":
                result = "won"
            elif r["result"] == "loss":
                result = "lost"
            tlegs.append({
                "abbr": _abbr(team, g.get("home_abbr") if leg["pick"] == "home" else g.get("away_abbr")),
                "opp_abbr": _abbr(opp, g.get("away_abbr") if leg["pick"] == "home" else g.get("home_abbr")),
                "score": "%d-%d" % (mine, theirs),
                "detail": detail,
                "state": r["state"],
                "result": result,
            })
        payload["tickets"].append({
            "label": ticket_label(tname),
            "status": status,
            "record": "%d-%d" % (wins, losses),
            "legs": tlegs,
        })
    return payload


def write_overlay(payload):
    d = os.path.dirname(OVERLAY_PATH)
    os.makedirs(d, exist_ok=True)
    tmp = OVERLAY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, OVERLAY_PATH)
    print("overlay -> %s (live=%s, tickets=%d)" % (
        OVERLAY_PATH, payload["live"], len(payload["tickets"])))


if __name__ == "__main__":
    main()
