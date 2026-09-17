"""Tests for the same-day-parlay Budz Picks rework: ticket structure,
same-day constraint, ESPN grading, idempotent history, recap, legacy seed.
Run from the project root: python -m pytest tests/test_budz_picks.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import budz_picks  # noqa: E402
import sportsbook  # noqa: E402

# Freeze "now" so game times are deterministic no matter when the suite runs.
_FROZEN_NOON = datetime(2026, 9, 17, 12, 0, tzinfo=budz_picks.ET)


class _FrozenDT(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return _FROZEN_NOON.replace(tzinfo=None)
        return _FROZEN_NOON.astimezone(tz)


budz_picks.datetime = _FrozenDT


def _fake_game_started(g):
    try:
        ct = datetime.fromisoformat(
            g["commence"].replace("Z", "+00:00"))
        return _FrozenDT.now(timezone.utc) >= ct
    except Exception:
        return False


sportsbook.game_started = _fake_game_started

_tmp = tempfile.mkdtemp(prefix="bb_picks_test_")
budz_picks.SOCIAL = _tmp
budz_picks.HISTORY_PATH = os.path.join(_tmp, "budz_picks_history.json")


def _iso_today(hour=19, day_offset=0):
    # Relative to the frozen noon: same ET calendar day, in the future.
    d = _FROZEN_NOON.date() + timedelta(days=day_offset)
    dt = datetime(d.year, d.month, d.day, hour, 5, tzinfo=budz_picks.ET)
    return dt.astimezone(timezone.utc).isoformat()


def _game(home, away, home_ml, away_ml, hour=19, day_offset=0, gid=None):
    return {
        "id": gid or ("%s-%s" % (away, home)),
        "home": home, "away": away,
        "commence": _iso_today(hour, day_offset),
        "ml": {"home": home_ml, "away": away_ml},
        "spread": {}, "total": {},
    }


def _raw(games, key="mlb"):
    return {key: (games, {"cached": True})}


def _leg(home, away, pick, odds, sport="mlb", market="ml", line=None,
         commence=None):
    return {
        "sport": sport, "home": home, "away": away, "pick": pick,
        "market": market, "line": line, "odds": odds,
        "desc": "%s ML (%s)" % (home if pick == "home" else away,
                                budz_picks.fmt_odds(odds)),
        "tip": "7:05 PM ET", "commence": commence or _iso_today(),
        "final": None, "leg_result": "pending", "analysis": None,
    }


def _espn(home, away, hs, aws, state="post"):
    return {"home": home, "away": away, "state": state,
            "home_score": hs, "away_score": aws}


# ---------- ticket construction ----------

def test_same_day_constraint():
    g_today = _game("Dodgers", "Reds", -200, 170, day_offset=0)
    g_tmrw = _game("Yankees", "Sox", -200, 170, day_offset=1)
    cands = budz_picks.gather_candidates(_raw([g_today, g_tmrw]))
    ids = {c["game"]["id"] for c in cands}
    assert g_today["id"] in ids
    assert g_tmrw["id"] not in ids


def test_two_tickets_structure_and_disjoint():
    games = [
        _game("T1H", "T1A", -200, 170, gid="g1"),
        _game("T2H", "T2A", -150, 130, gid="g2"),
        _game("T3H", "T3A", -110, 100, gid="g3"),
        _game("T4H", "T4A", -180, 160, gid="g4"),
        _game("T5H", "T5A", -250, 210, gid="g5"),
        _game("T6H", "T6A", -130, 115, gid="g6"),
        _game("T7H", "T7A", -300, 240, gid="g7"),
        _game("T8H", "T8A", -105, -105, gid="g8"),
    ]
    tickets = budz_picks.build_tickets(
        budz_picks.gather_candidates(_raw(games)))
    assert len(tickets) == 2
    safe, lotto = tickets
    assert safe["name"] == "Safe pick"
    assert lotto["name"] == "Lotto pick"
    assert 2 <= len(safe["legs"]) <= 3
    assert 3 <= len(lotto["legs"]) <= 5
    today = _FROZEN_NOON.date()
    seen = set()
    for t in tickets:
        for leg in t["legs"]:
            assert budz_picks.game_et_date(
                {"commence": leg["commence"]}) == today
            key = (leg["home"], leg["away"])
            assert key not in seen, "game used in both tickets"
            seen.add(key)
    # lotto combined odds are big
    assert int(lotto["combined_odds"].replace("+", "")) > 500


def test_thin_slate_no_forced_tickets():
    games = [_game("T1H", "T1A", -200, 170, gid="g1")]
    tickets = budz_picks.build_tickets(
        budz_picks.gather_candidates(_raw(games)))
    assert tickets == []


# ---------- grading ----------

def _history_with(legs_safe, legs_lotto):
    return {"entries": [{
        "date": "2026-09-15", "format": "v2",
        "tickets": [
            {"name": "Safe pick", "tagline": "", "legs": legs_safe,
             "combined_odds": "+150", "stake_budz": 50, "result": "pending"},
            {"name": "Lotto pick", "tagline": "", "legs": legs_lotto,
             "combined_odds": "+900", "stake_budz": 25, "result": "pending"},
        ],
        "record": {"wins": 0, "losses": 0, "net_budz": 0},
    }]}


def test_grading_win_and_bust_and_record():
    h = _history_with(
        [_leg("Dodgers", "Reds", "home", -200)],
        [_leg("Yankees", "Sox", "home", -150),
         _leg("Cards", "Cubs", "away", 180)],
    )
    boards = {"mlb": [
        _espn("Dodgers", "Reds", 7, 3),       # Dodgers ML wins
        _espn("Yankees", "Sox", 5, 2),        # Yankees ML wins
        _espn("Cards", "Cubs", 4, 1),         # Cubs ML loses
    ]}
    assert budz_picks.grade_history(h, boards=boards) is True
    t = h["entries"][0]["tickets"]
    assert t[0]["result"] == "win"
    assert t[1]["result"] == "bust"
    assert t[0]["legs"][0]["leg_result"] == "win"
    assert t[0]["legs"][0]["final"] == "Dodgers 7, Reds 3"
    assert "7-3" in t[0]["legs"][0]["analysis"]
    rec = h["entries"][0]["record"]
    assert rec["wins"] == 1 and rec["losses"] == 1
    # safe profit: 50 * 1.5 - 50 = 25; lotto bust: -25 -> net 0
    assert rec["net_budz"] == 0


def test_grading_is_idempotent():
    h = _history_with([_leg("Dodgers", "Reds", "home", -200)], [])
    boards = {"mlb": [_espn("Dodgers", "Reds", 7, 3)]}
    assert budz_picks.grade_history(h, boards=boards) is True
    rec1 = dict(h["entries"][0]["record"])
    assert budz_picks.grade_history(h, boards=boards) is False
    assert h["entries"][0]["record"] == rec1


def test_pending_legs_stay_pending():
    h = _history_with([_leg("Dodgers", "Reds", "home", -200)], [])
    boards = {"mlb": [_espn("Dodgers", "Reds", 0, 0, state="pre")]}
    assert budz_picks.grade_history(h, boards=boards) is False
    t = h["entries"][0]["tickets"][0]
    assert t["result"] == "pending"
    assert t["legs"][0]["leg_result"] == "pending"
    assert h["entries"][0]["record"] == {"wins": 0, "losses": 0,
                                        "net_budz": 0}


def test_push_excluded_from_profit():
    leg_win = _leg("Dodgers", "Reds", "home", -200)
    leg_push = _leg("Bills", "Jets", "home", -110, sport="nfl",
                    market="spread", line=-3.0)
    h = {"entries": [{
        "date": "2026-09-15", "format": "v2",
        "tickets": [{"name": "Safe pick", "tagline": "", "legs": [leg_win, leg_push],
                     "combined_odds": "+200", "stake_budz": 50,
                     "result": "pending"}],
        "record": {"wins": 0, "losses": 0, "net_budz": 0},
    }]}
    boards = {
        "mlb": [_espn("Dodgers", "Reds", 7, 3)],
        "nfl": [_espn("Bills", "Jets", 24, 21)],  # 24-3=21 -> push
    }
    assert budz_picks.grade_history(h, boards=boards) is True
    t = h["entries"][0]["tickets"][0]
    assert t["result"] == "win"
    assert leg_push["leg_result"] == "push"
    # profit counts only the winning leg: 50 * 1.5 - 50 = 25
    assert h["entries"][0]["record"] == {"wins": 1, "losses": 0,
                                        "net_budz": 25}


def test_void_after_72h_without_final():
    old = (datetime.now(timezone.utc) - timedelta(hours=80)).isoformat()
    leg = _leg("Dodgers", "Reds", "home", -200, commence=old)
    h = {"entries": [{
        "date": "2026-09-12", "format": "v2",
        "tickets": [{"name": "Safe pick", "tagline": "", "legs": [leg],
                     "combined_odds": "-200", "stake_budz": 50,
                     "result": "pending"}],
        "record": {"wins": 0, "losses": 0, "net_budz": 0},
    }]}
    assert budz_picks.grade_history(h, boards={"mlb": []}) is True
    t = h["entries"][0]["tickets"][0]
    assert leg["leg_result"] == "void"
    assert t["result"] == "void"
    assert h["entries"][0]["record"] == {"wins": 0, "losses": 0,
                                        "net_budz": 0}


def test_analysis_grounded_in_score():
    leg = _leg("Dodgers", "Reds", "home", -200)
    game = _espn("Dodgers", "Reds", 7, 3)
    a = budz_picks.analyze_leg(leg, game)
    assert "7-3" in a and "ML" in a
    leg2 = _leg("Bills", "Jets", "home", -110, sport="nfl",
                market="spread", line=-6.5)
    a2 = budz_picks.analyze_leg(leg2, _espn("Bills", "Jets", 31, 24))
    assert "31-24" in a2 and "-6.5" in a2 and "cover" in a2


# ---------- recap / sheet / legacy ----------

def test_recap_picks_yesterday():
    h = {"entries": [
        {"date": "2026-09-14", "tickets": [{"name": "Lotto pick"}],
         "record": {"wins": 0, "losses": 1, "net_budz": -25}},
        {"date": "2026-09-15", "tickets": [{"name": "Safe pick"}],
         "record": {"wins": 1, "losses": 1, "net_budz": 0}},
    ]}
    recap = budz_picks.build_recap(h, "2026-09-16")
    assert recap["date"] == "2026-09-15"
    assert recap["record"]["wins"] == 1
    assert budz_picks.build_recap(h, "2026-09-15")["date"] == "2026-09-14"


def test_seed_legacy_imports_old_sheet():
    old = {
        "date": "Wednesday, September 16, 2026",
        "picks": [
            {"sport": "NFL", "matchup": "Detroit Lions at Buffalo Bills",
             "tip": "Thu 9/17", "pick": "Buffalo Bills ML", "odds": "-220"},
            {"sport": "MLB", "matchup": "Los Angeles Dodgers at Cincinnati Reds",
             "tip": "Wed 9/16", "pick": "Los Angeles Dodgers ML", "odds": "-210"},
            {"sport": "MLB", "matchup": "Philadelphia Phillies at Washington Nationals",
             "tip": "Wed 9/16", "pick": "Philadelphia Phillies ML", "odds": "-196"},
        ],
        "parlay": {"legs": ["Bills ML (-220)", "Dodgers ML (-210)",
                            "Phillies ML (-196)"],
                   "combined_odds": "+224", "stake_budz": 50},
    }
    with open(os.path.join(_tmp, "budz_picks.json"), "w",
              encoding="utf-8") as f:
        json.dump(old, f)
    h = {"entries": []}
    assert budz_picks.seed_legacy(h) is True
    e = h["entries"][0]
    assert e["date"] == "2026-09-16"
    legs = e["tickets"][0]["legs"]
    assert len(legs) == 3
    assert legs[0]["home"] == "Buffalo Bills"
    assert legs[0]["pick"] == "home"
    assert legs[0]["odds"] == -220
    # new-format sheets are not imported
    h2 = {"entries": []}
    with open(os.path.join(_tmp, "budz_picks.json"), "w",
              encoding="utf-8") as f:
        json.dump({"tickets": []}, f)
    assert budz_picks.seed_legacy(h2) is False


def test_build_sheet_end_to_end():
    games = [
        _game("T1H", "T1A", -200, 170, gid="g1"),
        _game("T2H", "T2A", -150, 130, gid="g2"),
        _game("T3H", "T3A", -110, 100, gid="g3"),
        _game("T4H", "T4A", -180, 160, gid="g4"),
        _game("T5H", "T5A", -250, 210, gid="g5"),
        _game("T6H", "T6A", -130, 115, gid="g6"),
        _game("T7H", "T7A", -300, 240, gid="g7"),
        _game("T8H", "T8A", -105, -105, gid="g8"),
    ]
    if os.path.exists(budz_picks.HISTORY_PATH):
        os.remove(budz_picks.HISTORY_PATH)
    sheet = budz_picks.build_sheet(boards={}, raw_games=_raw(games))
    assert len(sheet["tickets"]) == 2
    assert sheet["recap"] is None  # nothing yesterday on a fresh history
    assert "same-day" in sheet["note"]
    h = budz_picks.load_history()
    today = "2026-09-17"
    assert budz_picks.entry_for(h, today) is not None
    # second build same day: no duplicate entry, tickets unchanged
    sheet2 = budz_picks.build_sheet(boards={}, raw_games=_raw(games))
    h2 = budz_picks.load_history()
    assert len([e for e in h2["entries"] if e["date"] == today]) == 1
    assert sheet2["tickets"][0]["legs"][0]["desc"] == \
        sheet["tickets"][0]["legs"][0]["desc"]


def test_espn_fallback_hosts():
    calls = []
    real = sportsbook._http_get_json

    def fake(url, timeout=20):
        calls.append(url)
        if url.startswith("https://site.api.espn.com"):
            raise Exception("403 Forbidden")
        return {"events": []}

    sportsbook._http_get_json = fake
    try:
        assert budz_picks._fetch_espn_board("mlb") == []
        assert calls[0].startswith("https://site.api.espn.com")
        assert any("site.web.api.espn.com" in u for u in calls[1:])
    finally:
        sportsbook._http_get_json = real


def test_parse_espn_board():
    data = {"events": [{
        "status": {"type": {"state": "post"}},
        "competitions": [{"competitors": [
            {"homeAway": "home",
             "team": {"displayName": "Los Angeles Dodgers"}, "score": "7"},
            {"homeAway": "away",
             "team": {"displayName": "Cincinnati Reds"}, "score": "3"},
        ]}],
    }]}
    games = budz_picks._parse_espn_board(data)
    assert games == [{"home": "Los Angeles Dodgers", "away": "Cincinnati Reds",
                      "state": "post", "home_score": 7, "away_score": 3}]
