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
import os
import json
import time
import urllib.request

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/126.0 Safari/537.36"}

# --- ESPN endpoints (403 on some networks; NFL has theScore fallback) ---
_ESPN_LEAGUES = {
    "NFL": "football/nfl/scoreboard",
    "NBA": "basketball/nba/scoreboard",
    "NHL": "hockey/nhl/scoreboard",
}
_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/"

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

# ---------------------------------------------------------------- spreads
# Pre-game spread lines, read-only from the T.H.C. sportsbook's odds cache
# (FanDuel lines — the same board as $lines in chat). Never triggers an
# Odds API refresh, so no quota is burned.
_SOCIAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ODDS_CACHE = os.path.normpath(
    os.path.join(_SOCIAL, "..", "thc-test", "odds_cache.json"))


def _load_spread_lines():
    """{full team name: spread line}. Negative = that team is favored."""
    try:
        with open(_ODDS_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        return {}
    lines = {}
    for sport in (cache.get("sports") or {}).values():
        for g in (sport.get("games") or []):
            try:
                spread = g.get("spread") or {}
                if "home_line" in spread:
                    lines[g["home"]] = float(spread["home_line"])
                if "away_line" in spread:
                    lines[g["away"]] = float(spread["away_line"])
            except (KeyError, TypeError, ValueError):
                continue
    return lines


def cover_result(home_score, away_score, home_line):
    """'home' / 'away' / 'push' / None: who is covering the spread."""
    try:
        margin = int(home_score) - int(away_score)
        line = float(home_line)
    except (TypeError, ValueError):
        return None
    diff = margin + line
    if diff > 0:
        return "home"
    if diff < 0:
        return "away"
    return "push"


def _attach_lines(games):
    """Add 'spread' (home perspective) + 'covering' to each game dict."""
    lines = _load_spread_lines()
    for g in games:
        home = g.get("home") or {}
        away = g.get("away") or {}
        line = lines.get(home.get("name", ""))
        g["spread"] = line
        if line is not None and g.get("state") in ("in", "post"):
            g["covering"] = cover_result(home.get("score", 0),
                                         away.get("score", 0), line)
        else:
            g["covering"] = None
    return games


def _http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- ESPN
def _parse_espn_game(ev, league="NFL"):
    comp = (ev.get("competitions") or [{}])[0]
    status = ev.get("status") or {}
    stype = status.get("type") or {}
    game = {
        "id": "espn-" + str(ev.get("id", "")),
        "league": league,
        "state": stype.get("state", "pre"),
        "clock": status.get("displayClock", ""),
        "period": status.get("period", 0),
        "date": ev.get("date", ""),
        "detail": stype.get("shortDetail", "") or stype.get("detail", ""),
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


def _espn_league(league):
    """ESPN scoreboard for one league (NFL/NBA/NHL). [] on any failure."""
    try:
        data = _http_get_json(_ESPN_BASE + _ESPN_LEAGUES[league])
    except Exception:
        return []
    return [_parse_espn_game(ev, league)
            for ev in data.get("events", []) or []]


def _espn_nfl():
    return _espn_league("NFL")


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
    """NFL + NBA + NHL + MLB games for the ticker.

    Cached 60s; stale data served on failure. Every game also carries
    'spread' (pre-game line, home-team perspective) and 'covering'
    ('home'/'away'/'push'/None) for live and final games.
    """
    now = time.time()
    if now - _scores_cache["at"] < _SCORES_TTL and _scores_cache["data"]:
        return _scores_cache["data"]
    try:
        nfl = _espn_nfl() or _thescore_nfl()
    except Exception:
        nfl = []
    extra = []
    for lg in ("NBA", "NHL"):
        try:
            extra.extend(_espn_league(lg))
        except Exception:
            pass
    try:
        mlb = _mlb()
    except Exception:
        mlb = []
    games = _attach_lines((nfl or []) + extra + (mlb or []))
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
            "detail": g.get("detail", ""),
            "spread": g.get("spread"),
            "covering": g.get("covering"),
            "leaders": leaders,
        })
    if out:
        _look_cache["data"] = out
        _look_cache["at"] = now
    return _look_cache["data"]
