"""Daily Budz Picks generator.

Windows Scheduled Task `BudzPicksDaily` runs this every morning at 8 AM ET:
    C:\\Users\\Starc\\AppData\\Local\\Programs\\Python\\Python312\\python.exe
        C:\\Users\\Starc\\thc-social\\budz_picks.py

- Loads ODDS_API_KEY from C:\\Users\\Starc\\thc-test\\.env (never printed).
- Reuses the T.H.C. sportsbook for cached odds (quota-safe: 3h cache,
  14 calls/day cap) so the sheet matches the $lines board.
- Writes budz_picks.json (rendered by the /picks page).
- Posts the sheet summary to the BudzBook feed as @jarvis.

The sheet is framed honestly: leans, not locks. Play money only.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

SOCIAL = r'C:\Users\Starc\thc-social'
THC = r'C:\Users\Starc\thc-test'
sys.path.insert(0, THC)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(THC, '.env'))  # ODDS_API_KEY; never printed
except ImportError:
    pass

import sportsbook

SPORT_NAMES = {}  # filled from sportsbook.SPORTS at runtime
# EDT (UTC-4) — fine for the baseball/football season window.
ET = timezone(timedelta(hours=-4))
STAKE_BUDZ = 50


def american_to_prob(o):
    o = int(o)
    return (-o / (-o + 100.0)) if o < 0 else (100.0 / (o + 100.0))


def fmt_odds(o):
    o = int(o)
    return ('%+d' % o)


def fmt_tip(iso):
    # Portable (Windows-safe) ET tip-off formatting; no %-directives.
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(ET)
        hr = dt.hour % 12 or 12
        return '%s %d/%d %d:%02d %s ET' % (
            dt.strftime('%a'), dt.month, dt.day, hr, dt.minute, dt.strftime('%p'))
    except Exception:
        return ''


def within_hours(game, hours):
    try:
        dt = datetime.fromisoformat(game['commence'].replace('Z', '+00:00'))
    except Exception:
        return False
    now = datetime.now(timezone.utc)
    return timedelta(0) < (dt - now) < timedelta(hours=hours)


def favorite_side(game):
    ml = game.get('ml', {})
    if 'home' not in ml or 'away' not in ml:
        return None
    # shorter price = favorite
    return 'home' if american_to_prob(ml['home']) >= american_to_prob(ml['away']) else 'away'


FAV_BLURBS = [
    "Chalky, but the books don't hand out {odds} for nothing. {team} handles business at home{road}.",
    "Not cute, just correct. {team} is the better side and {odds} is a fair price to say so.",
    "Sometimes the obvious answer is obvious. {team} wins this more often than not.",
]
DOG_BLURBS = [
    "The dart throw. {team} at {odds} — nobody believes, which is exactly when it's fun.",
    "Sprinkle pick. {team} probably loses, but at {odds} you only need to be right sometimes.",
]


def build_sheet():
    raw = sportsbook.get_all_games() or {}
    for _k, _v in sportsbook.SPORTS.items():
        SPORT_NAMES[_k] = _v.get('label', _k.upper())
    cands = []
    for sport_key, val in raw.items():
        # get_all_games() -> {sport: (games, meta)}
        games = val[0] if isinstance(val, (tuple, list)) and len(val) == 2 and isinstance(val[0], list) else val
        sport = SPORT_NAMES.get(sport_key, sport_key.upper())
        for g in games or []:
            try:
                if sportsbook.game_started(g):
                    continue
            except Exception:
                pass
            if not within_hours(g, 60):
                continue
            side = favorite_side(g)
            if not side:
                continue
            fav_odds = int(g['ml'][side])
            dog_side = 'away' if side == 'home' else 'home'
            cands.append({
                'sport': sport, 'sport_key': sport_key, 'game': g,
                'fav_side': side, 'fav_odds': fav_odds,
                'dog_side': dog_side, 'dog_odds': int(g['ml'][dog_side]),
                'fav_prob': american_to_prob(fav_odds),
            })
    cands.sort(key=lambda c: -c['fav_prob'])

    picks = []
    parlay_legs = []
    # Bankers: 3 shortest-odds favorites in the sane-chalk band.
    bankers = [c for c in cands if -260 <= c['fav_odds'] <= -105][:3]
    for i, c in enumerate(bankers):
        g = c['game']
        team = g['home'] if c['fav_side'] == 'home' else g['away']
        road = '' if c['fav_side'] == 'home' else ' on the road'
        blurb = FAV_BLURBS[i % len(FAV_BLURBS)].format(
            team=team, odds=fmt_odds(c['fav_odds']), road=road)
        matchup = "%s at %s" % (g['away'], g['home'])
        picks.append({
            'sport': c['sport'], 'matchup': matchup, 'tip': fmt_tip(g['commence']),
            'pick': '%s ML' % team, 'odds': fmt_odds(c['fav_odds']),
            'blurb': blurb,
            'stars': '⭐⭐⭐' if c['fav_prob'] >= 0.65 else '⭐⭐',
            'tag': 'banker',
        })
        try:
            parlay_legs.append(sportsbook.build_leg(g, c['sport_key'], c['fav_side'], 'ml'))
        except Exception:
            pass
    # Dart: the longest underdog at +250 or shorter, from a different game
    # than the bankers (no picking both sides of one game on the sheet).
    used_ids = {c['game'].get('id') for c in bankers}
    dogs = [c for c in cands
            if c['dog_odds'] <= 250 and c['game'].get('id') not in used_ids]
    if dogs:
        c = max(dogs, key=lambda c: c['dog_odds'])
        g = c['game']
        team = g['home'] if c['dog_side'] == 'home' else g['away']
        matchup = "%s at %s" % (g['away'], g['home'])
        picks.append({
            'sport': c['sport'], 'matchup': matchup, 'tip': fmt_tip(g['commence']),
            'pick': '%s ML' % team, 'odds': fmt_odds(c['dog_odds']),
            'blurb': DOG_BLURBS[len(bankers) % len(DOG_BLURBS)].format(
                team=team, odds=fmt_odds(c['dog_odds'])),
            'stars': '⭐', 'tag': 'dart',
        })

    parlay = None
    if len(parlay_legs) >= 2:
        try:
            combined = sportsbook.decimal_to_american(sportsbook.parlay_decimal(parlay_legs))
            parlay = {
                'legs': [sportsbook.describe_leg(l) for l in parlay_legs],
                'combined_odds': fmt_odds(combined),
                'stake_budz': STAKE_BUDZ,
            }
        except Exception:
            parlay = None

    today = datetime.now(ET).strftime('%A, %B %d, %Y')
    return {
        'date': today,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'picks': picks,
        'parlay': parlay,
        'note': ("Jarvis's leans for the slate — bankers plus one dart. "
                 "Leans, not locks. Play-money BUDZ only.") if picks else
                "No board for the next 60 hours — the sheet will be back when games are.",
    }


def post_to_feed(sheet):
    if not sheet['picks']:
        return
    legs_txt = ''
    if sheet['parlay']:
        legs_txt = ' Parlay: ' + ' / '.join(sheet['parlay']['legs'][:3])
        legs_txt += ' (%s).' % sheet['parlay']['combined_odds']
    body = ("🎯 Today's Budz Picks are live! %d leans up%s Full sheet: /picks"
            % (len(sheet['picks']), legs_txt))
    if len(body) > 280:
        body = body[:277] + '…'
    db_path = os.path.join(SOCIAL, 'thc_social.db')
    con = sqlite3.connect(db_path, timeout=15)
    try:
        cur = con.execute("SELECT id FROM users WHERE username = 'jarvis'")
        row = cur.fetchone()
        if not row:
            print('feed post skipped: no jarvis user')
            return
        # One picks post per day — don't double-post on re-runs.
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        cur = con.execute(
            "SELECT 1 FROM posts WHERE user_id = ? AND kind = 'post'"
            " AND substr(created_at, 1, 10) = ? AND body LIKE '%Budz Picks are live%'",
            (row[0], today))
        if cur.fetchone():
            print('feed post skipped: already posted today')
            return
        now = datetime.now(timezone.utc).isoformat()
        con.execute(
            "INSERT INTO posts (user_id, body, image_path, kind, created_at)"
            " VALUES (?, ?, NULL, 'post', ?)", (row[0], body, now))
        con.commit()
        print('feed post by @jarvis published')
    finally:
        con.close()


def main():
    sheet = build_sheet()
    out = os.path.join(SOCIAL, 'budz_picks.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(sheet, f, ensure_ascii=False, indent=2)
    print('wrote %s (%d picks)' % (out, len(sheet['picks'])))
    if sheet['picks']:
        post_to_feed(sheet)


if __name__ == '__main__':
    main()
