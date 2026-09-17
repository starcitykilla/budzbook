"""Tests for the live score ticker's spread/cover additions (app/ticker.py)."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import ticker


def test_cover_result_home_covers():
    assert ticker.cover_result(24, 17, -4.5) == "home"


def test_cover_result_away_covers():
    assert ticker.cover_result(21, 17, -4.5) == "away"


def test_cover_result_dog_covers():
    assert ticker.cover_result(20, 17, 3.5) == "home"


def test_cover_result_push():
    assert ticker.cover_result(21, 17, -4.0) == "push"


def test_cover_result_bad_input():
    assert ticker.cover_result("x", 17, -4.5) is None
    assert ticker.cover_result(21, 17, None) is None


def _game(home_name, away_name, hs, aws, state="in"):
    return {"league": "NFL", "state": state, "clock": "Q3 4:12", "period": 3,
            "date": "", "home": {"name": home_name, "abbr": "H", "score": hs},
            "away": {"name": away_name, "abbr": "A", "score": aws}}


def test_attach_lines_uses_odds_cache(monkeypatch, tmp_path):
    cache = {"sports": {"nfl": {"games": [
        {"home": "Buffalo Bills", "away": "Detroit Lions",
         "spread": {"home_line": -4.5, "away_line": 4.5}}]}}}
    f = tmp_path / "odds_cache.json"
    f.write_text(json.dumps(cache), encoding="utf-8")
    monkeypatch.setattr(ticker, "_ODDS_CACHE", str(f))
    games = ticker._attach_lines([_game("Buffalo Bills", "Detroit Lions", 24, 17)])
    assert games[0]["spread"] == -4.5
    assert games[0]["covering"] == "home"


def test_attach_lines_pregame_no_covering(monkeypatch, tmp_path):
    cache = {"sports": {"nfl": {"games": [
        {"home": "Buffalo Bills", "away": "Detroit Lions",
         "spread": {"home_line": -4.5, "away_line": 4.5}}]}}}
    f = tmp_path / "odds_cache.json"
    f.write_text(json.dumps(cache), encoding="utf-8")
    monkeypatch.setattr(ticker, "_ODDS_CACHE", str(f))
    games = ticker._attach_lines(
        [_game("Buffalo Bills", "Detroit Lions", 0, 0, state="pre")])
    assert games[0]["spread"] == -4.5
    assert games[0]["covering"] is None


def test_attach_lines_missing_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(ticker, "_ODDS_CACHE", str(tmp_path / "nope.json"))
    games = ticker._attach_lines([_game("Buffalo Bills", "Detroit Lions", 24, 17)])
    assert games[0]["spread"] is None
    assert games[0]["covering"] is None


def _espn_event():
    return {
        "id": "1",
        "date": "2026-09-16T23:00Z",
        "status": {"displayClock": "4:12", "period": 3,
                   "type": {"state": "in", "shortDetail": "Q3 4:12"}},
        "competitions": [{"competitors": [
            {"homeAway": "home",
             "team": {"abbreviation": "BUF", "displayName": "Buffalo Bills"},
             "score": "24"},
            {"homeAway": "away",
             "team": {"abbreviation": "DET", "displayName": "Detroit Lions"},
             "score": "17"},
        ]}],
    }


def test_parse_espn_game_league_param():
    g = ticker._parse_espn_game(_espn_event(), "NBA")
    assert g["league"] == "NBA"
    assert g["state"] == "in"
    assert g["home"]["abbr"] == "BUF"
    assert g["home"]["score"] == 24
    assert g["detail"] == "Q3 4:12"


def test_get_scores_carries_spread_fields(monkeypatch):
    fake = [_game("Buffalo Bills", "Detroit Lions", 24, 17)]
    monkeypatch.setattr(ticker, "_espn_league", lambda lg: fake if lg == "NFL" else [])
    monkeypatch.setattr(ticker, "_thescore_nfl", lambda: [])
    monkeypatch.setattr(ticker, "_mlb", lambda: [])
    monkeypatch.setattr(ticker, "_attach_lines", lambda gs: [
        dict(g, spread=-4.5, covering="home") for g in gs])
    ticker._scores_cache["at"] = 0
    ticker._scores_cache["data"] = []
    games = ticker.get_scores()
    assert games and games[0]["spread"] == -4.5
    assert games[0]["covering"] == "home"
