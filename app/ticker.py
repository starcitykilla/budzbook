"""Stream overlay data: multi-league score ticker + live-look player highlights.

Stdlib only. In-memory caches so Brad's OBS browser sources can poll
without hammering upstream APIs. Called from FastAPI routes in main.py.

Sources (first success wins per league):
  NFL: ESPN site API -> theScore public API (ESPN 403s some networks)
  MLB: statsapi.mlb.com (free, no key; in season Aug-Oct)

Every game dict: id, league, state (pre|in|post), clock, period, date,
                 home {name, abbr, score}, away {name, abbr, score}.
Live-look "leaders" are latest scoring plays: team, category, name, line.
"""

import csv  # noqa: F401  (kept for future nflverse use)
import datetime
import json
import time
import urllib.request

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/126.0 Safari/537.36"}

# --- legacy ESPN endpoints (kept first; 403 on some networks) ---
_ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/nfl/scoreboard"

# --- theScore NFL fallback ---
_THESCORE_EVENTS = "https://api.thescore.com/nfl/events"
_THESCORE_EVENT = "https://api.thescore.com/football/events/%s"

# --- MLB ---
_MLB_SCHEDULE = ("https://statsapi.mlb.com/api/v1/schedule"
                 "?sportId=1&date=%s&hydrate=team,linescore")

_scores_cache = {"at": 0.0, "data": []}
_look_cache = {"at": 0.0, "data": []}
_detail_cache = {}  # event_id -> (at, scoring_plays)
_SCORES_TTL = 60.0
_LOOK_TTL = 120.0
_DETAIL_TTL = 600.0


def _http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- ESPN (NFL)
def _parse_espn_game(ev):
    comp = (ev.get("competitions") or [{}])[0]
    status = ev.get("status") or {}
    stype = status.get("type") or {}
    game = {
        "id": "espn-" + str(ev.get("id", "")),
        "league": "NFL",
        "state": stype.get("state", "pre"),
        "clock": status.get("displayClock", ""),
        "period": status.get("period", 0),
        "date": ev.get("date", ""),
    }
    for c in comp.get("competitors", []) or []:
        team = c.get("team") or {}
        try:
            score = int(c.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        entry = {"name": team.get("displayName", ""),
                 "abbr": team.get("abbreviation", ""), "score": score}
        if c.get("homeAway") == "home":
            game["home"] = entry
        else:
            game["away"] = entry
    game.setdefault("home", {"name": "", "abbr": "", "score": 0})
    game.setdefault("away", {"name": "", "abbr": "", "score": 0})
    return game


def _espn_nfl():
    try:
        data = _http_get_json(_ESPN_SCOREBOARD)
    except Exception:
        return []
    return [_parse_espn_game(ev) for ev in data.get("events", []) or []]


# ------------------------------------------------------- theScore NFL fallback
_TS_STATE = {"pre_game": "pre", "in_progress": "in", "final": "post"}


def _ts_parse_date(s):
    # "Sun, 13 Sep 2026 17:00:00 -0000"
    try:
        return datetime.datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z")
    except (TypeError, ValueError):
        return None


def _ts_game(ev, now):
    status = ev.get("event_status") or ""
    state = _TS_STATE.get(status)
    if state is None:
        state = "in" if "progress" in status or "delay" in status else "pre"
    dt = _ts_parse_date(ev.get("game_date") or "")
    if dt is None:
        return None
    # window: last 4 days .. next 8 days
    if not (now - datetime.timedelta(days=4) <= dt
            <= now + datetime.timedelta(days=8)):
        return None
    bs = ev.get("box_score") or {}
    sc = bs.get("score") or {}
    prog = bs.get("progress") or {}
    game = {
        "id": "ts-" + str(ev.get("id", "")),
        "league": "NFL",
        "state": state,
        "clock": prog.get("clock_label", "") or "",
        "period": prog.get("segment", 0) or 0,
        "date": dt.isoformat(),
        "_sort": dt.timestamp(),
    }
    for side, key in (("home", "home_team"), ("away", "away_team")):
        t = ev.get(key) or {}
        try:
            score = int(((sc.get(side) or {}).get("score")) or 0)
        except (TypeError, ValueError):
            score = 0
        game[side] = {"name": t.get("full_name", "") or
                      ((t.get("location") or "") + " " + (t.get("name") or "")).strip(),
                      "abbr": t.get("abbreviation", ""), "score": score}
    return game


def _thescore_nfl():
    try:
        evs = _http_get_json(_THESCORE_EVENTS, timeout=20)
    except Exception:
        return []
    now = datetime.datetime.now(datetime.timezone.utc)
    games = []
    for ev in evs or []:
        try:
            g = _ts_game(ev, now)
        except Exception:
            continue
        if g:
            games.append(g)
    order = {"in": 0, "post": 1, "pre": 2}
    games.sort(key=lambda g: (order.get(g["state"], 3),
                              -g["_sort"] if g["state"] == "post" else g["_sort"]))
    for g in games:
        g.pop("_sort", None)
    return games


def _ts_scoring_plays(event_id):
    """Latest scoring plays for a theScore event -> leader-style entries."""
    now = time.time()
    hit = _detail_cache.get(event_id)
    if hit and now - hit[0] < _DETAIL_TTL:
        return hit[1]
    plays = []
    try:
        d = _http_get_json(_THESCORE_EVENT % event_id, timeout=15)
        ss = (d.get("box_score") or {}).get("scoring_summary") or []
        for p in ss[-6:][::-1]:  # newest first, max 6
            scorer = p.get("scorer") or {}
            name = (scorer.get("first_initial_and_last_name")
                    or scorer.get("full_name") or "").strip()
            desc = (p.get("score_type_description") or "").strip()
            q = p.get("quarter") or p.get("segment") or 0
            try:
                dist = int(p.get("distance") or 0)
            except (TypeError, ValueError):
                dist = 0
            cat = ("Q%d" % q if q and q <= 4 else "OT") + (
                " \u00b7 " + desc if desc else "")
            line = ("%d-yd " % dist if dist else "") + desc
            plays.append({"team": "", "category": cat,
                          "name": name or "Team score", "line": line or desc})
    except Exception:
        pass
    _detail_cache[event_id] = (now, plays)
    return plays


# ------------------------------------------------------------------- MLB
def _mlb_games(day):
    try:
        data = _http_get_json(_MLB_SCHEDULE % day, timeout=15)
    except Exception:
        return []
    games = []
    dates = data.get("dates") or []
    for d in dates:
        for g in d.get("games") or []:
            status = g.get("status") or {}
            abstract = (status.get("abstractGameState") or "").lower()
            if abstract == "live":
                state = "in"
            elif abstract == "final":
                state = "post"
            else:
                state = "pre"
            ls = g.get("linescore") or {}
            if state == "in":
                clock = "%s %s" % (ls.get("inningState", ""),
                                   ls.get("currentInningOrdinal", ""))
                clock = (clock.strip() + " \u00b7 %d OUT" % (ls.get("outs", 0))
                         if ls.get("outs") is not None else clock.strip())
                period = ls.get("currentInning", 0) or 0
            else:
                clock, period = "", 0
            game = {
                "id": "mlb-" + str(g.get("gamePk", "")),
                "league": "MLB",
                "state": state,
                "clock": clock,
                "period": period,
                "date": g.get("gameDate", ""),
            }
            for side in ("home", "away"):
                t = ((g.get("teams") or {}).get(side) or {})
                info = t.get("team") or {}
                try:
                    score = int(t.get("score") or 0)
                except (TypeError, ValueError):
                    score = 0
                game[side] = {"name": info.get("name", ""),
                              "abbr": info.get("abbreviation", ""),
                              "score": score}
            games.append(game)
    order = {"in": 0, "post": 1, "pre": 2}
    games.sort(key=lambda x: order.get(x["state"], 3))
    return games


def _mlb():
    today = datetime.date.today()
    days = [(today + datetime.timedelta(days=d)).isoformat() for d in (-1, 0, 1)]
    out = []
    for day in days:
        out.extend(_mlb_games(day))
    # de-dupe by id, keep first
    seen, uniq = set(), []
    for g in out:
        if g["id"] not in seen:
            seen.add(g["id"])
            uniq.append(g)
    order = {"in": 0, "post": 1, "pre": 2}
    uniq.sort(key=lambda x: order.get(x["state"], 3))
    return uniq


# ------------------------------------------------------------------ public
def get_scores():
    """NFL + MLB games for the ticker. Cached 60s; stale data on failure."""
    now = time.time()
    if now - _scores_cache["at"] < _SCORES_TTL and _scores_cache["data"]:
        return _scores_cache["data"]
    try:
        nfl = _espn_nfl() or _thescore_nfl()
    except Exception:
        nfl = []
    try:
        mlb = _mlb()
    except Exception:
        mlb = []
    games = (nfl or []) + (mlb or [])
    if games:
        _scores_cache["data"] = games
        _scores_cache["at"] = now
    return _scores_cache["data"]


def get_live_look():
    """Games with latest scoring-play highlights for the live-look window.

    Live games first, then finals, then upcoming. Cached 120s.
    """
    now = time.time()
    if now - _look_cache["at"] < _LOOK_TTL and _look_cache["data"]:
        return _look_cache["data"]
    games = get_scores()
    # rotation: live games first (cap 4 so finals w/ highlights still show),
    # then finals and upcoming with NFL ahead of MLB inside each state.
    by_state = {"in": [], "post": [], "pre": []}
    for g in games:
        by_state.get(g["state"], by_state["pre"]).append(g)
    for s in by_state:
        by_state[s].sort(key=lambda g: 0 if g.get("league") == "NFL" else 1)
    ordered = by_state["in"][:4] + by_state["post"] + by_state["pre"]
    games = ordered[:8]
    out = []
    for g in games:
        leaders = []
        if g.get("league") == "NFL" and g["state"] in ("in", "post") \
                and str(g.get("id", "")).startswith("ts-"):
            leaders = _ts_scoring_plays(g["id"][3:])
        out.append({
            "away": g["away"],
            "home": g["home"],
            "league": g.get("league", "NFL"),
            "state": g["state"],
            "clock": g["clock"],
            "period": g["period"],
            "date": g["date"],
            "leaders": leaders,
        })
    if out:
        _look_cache["data"] = out
        _look_cache["at"] = now
    return _look_cache["data"]
