"""Daily Budz Picks generator — same-day parlay edition.

Windows Scheduled Task `BudzPicksDaily` runs this every morning at 8 AM ET:
    C:\\Users\\Starc\\AppData\\Local\\Programs\\Python\\Python312\\python.exe
        C:\\Users\\Starc\\thc-social\\budz_picks.py

- Loads ODDS_API_KEY from C:\\Users\\Starc\\thc-test\\.env (never printed).
- Reuses the T.H.C. sportsbook for cached odds (quota-safe: 3h cache,
  14 calls/day cap) so the sheet matches the $lines board.
- Settles picks against ESPN's free scoreboard (no key, no quota),
  same source the sportsbook uses for bet settlement.
- Writes budz_picks.json (rendered by the /picks page).
- Appends to budz_picks_history.json: every ticket ever posted, graded
  win/bust, with a running record. Grading is idempotent — a day graded
  once is never double-counted. Legs that aren't final yet stay pending
  and are finalized on the next run.
- Posts the sheet + yesterday's recap to the BudzBook feed as @jarvis.

The sheet is framed honestly: leans, not locks. Play money only.
These are posted picks with suggested stakes — the script never places
real sportsbook wagers.
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
LABEL_TO_KEY = {'NFL': 'nfl', 'MLB': 'mlb', 'NHL': 'nhl', 'NBA': 'nba'}
# EDT (UTC-4) — fine for the baseball/football season window.
ET = timezone(timedelta(hours=-4))

SAFE_STAKE = 50
LOTTO_STAKE = 25
HISTORY_PATH = os.path.join(SOCIAL, 'budz_picks_history.json')

RESULT_EMOJI = {'win': '✅', 'bust': '❌', 'pending': '⏳', 'push': '🤝',
                'void': '🚫'}


def american_to_prob(o):
    o = int(o)
    return (-o / (-o + 100.0)) if o < 0 else (100.0 / (o + 100.0))


def fmt_odds(o):
    o = int(o)
    return '%+d' % o


def fmt_tip(iso):
    # Portable (Windows-safe) ET tip-off formatting; no %-directives.
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(ET)
        hr = dt.hour % 12 or 12
        return '%s %d/%d %d:%02d %s ET' % (
            dt.strftime('%a'), dt.month, dt.day, hr, dt.minute, dt.strftime('%p'))
    except Exception:
        return ''


def game_et_date(game):
    """Calendar date (ET) of a game's commence time. None if unparseable."""
    try:
        return datetime.fromisoformat(
            game['commence'].replace('Z', '+00:00')).astimezone(ET).date()
    except Exception:
        return None


def favorite_side(game):
    ml = game.get('ml', {})
    if 'home' not in ml or 'away' not in ml:
        return None
    # shorter price = favorite
    return 'home' if american_to_prob(ml['home']) >= american_to_prob(ml['away']) else 'away'


def gather_candidates(raw):
    """Unstarted games tipping off today (ET) with a moneyline on both sides."""
    today = datetime.now(ET).date()
    cands = []
    for sport_key, val in raw.items():
        # get_all_games() -> {sport: (games, meta)}
        games = val[0] if isinstance(val, (tuple, list)) and len(val) == 2 and isinstance(val[0], list) else val
        for g in games or []:
            try:
                if sportsbook.game_started(g):
                    continue
            except Exception:
                pass
            if game_et_date(g) != today:
                continue
            side = favorite_side(g)
            if not side:
                continue
            fav_odds = int(g['ml'][side])
            dog_side = 'away' if side == 'home' else 'home'
            cands.append({
                'sport': SPORT_NAMES.get(sport_key, sport_key.upper()),
                'sport_key': sport_key, 'game': g,
                'fav_side': side, 'fav_odds': fav_odds,
                'dog_side': dog_side, 'dog_odds': int(g['ml'][dog_side]),
                'fav_prob': american_to_prob(fav_odds),
            })
    return cands


def _leg_entry(c, sport_key, side, market):
    """Build a history-style leg dict from a candidate game."""
    g = c['game']
    leg = sportsbook.build_leg(g, sport_key, side, market)
    team = g['home'] if side == 'home' else g['away']
    return {
        'sport': sport_key, 'home': g['home'], 'away': g['away'],
        'pick': side, 'market': market, 'line': leg['line'],
        'odds': int(leg['odds']),
        'desc': '%s ML (%s)' % (team, fmt_odds(leg['odds'])),
        'tip': fmt_tip(g['commence']),
        'commence': g['commence'],
        'final': None, 'leg_result': 'pending', 'analysis': None,
    }


def build_tickets(cands):
    """Exactly the 2 daily tickets: Safe pick (2-3 chalk legs) + Lotto pick
    (3-5 legs, big odds, every leg a realistic hit). Fewer if the slate is
    too thin — never pulls legs from another day."""
    tickets = []
    used = set()

    def use(c):
        used.add(c['game'].get('id'))

    # --- Safe pick: 2-3 shortest-odds favorites in the sane-chalk band.
    bankers = sorted(
        (c for c in cands if -260 <= c['fav_odds'] <= -105),
        key=lambda c: -c['fav_prob'])[:3]
    if len(bankers) >= 2:
        legs = []
        for c in bankers:
            try:
                legs.append(_leg_entry(c, c['sport_key'], c['fav_side'], 'ml'))
            except Exception:
                continue
        if len(legs) >= 2:
            for c in bankers:
                use(c)
            combined = sportsbook.decimal_to_american(
                sportsbook.parlay_decimal(
                    [{'odds': l['odds']} for l in legs]))
            tickets.append({
                'name': 'Safe pick',
                'tagline': 'The steadier ticket — chalk that should hold.',
                'legs': legs, 'combined_odds': fmt_odds(combined),
                'stake_budz': SAFE_STAKE, 'result': 'pending',
            })

    # --- Lotto pick: 3-5 legs, big combined odds, realistic legs only.
    pool = [c for c in cands if c['game'].get('id') not in used]
    # Realistic underdogs first (shortest plus-odds in the +250 band),
    # then the shortest favorites to fill out.
    dogs = sorted(
        (c for c in pool if 0 < c['dog_odds'] <= 250),
        key=lambda c: c['dog_odds'])[:2]
    favs = sorted(
        (c for c in pool if c not in dogs and -300 <= c['fav_odds'] <= -105),
        key=lambda c: -c['fav_prob'])
    lotto_cands = (dogs + favs)[:5]
    if len(lotto_cands) >= 3:
        legs = []
        for c in lotto_cands:
            side = c['dog_side'] if c in dogs else c['fav_side']
            try:
                legs.append(_leg_entry(c, c['sport_key'], side, 'ml'))
            except Exception:
                continue
        if len(legs) >= 3:
            combined = sportsbook.decimal_to_american(
                sportsbook.parlay_decimal(
                    [{'odds': l['odds']} for l in legs]))
            tickets.append({
                'name': 'Lotto pick',
                'tagline': 'Big odds, but every leg has a real path.',
                'legs': legs, 'combined_odds': fmt_odds(combined),
                'stake_budz': LOTTO_STAKE, 'result': 'pending',
            })
    return tickets


# ---------------- history ----------------

def load_history():
    try:
        with open(HISTORY_PATH, 'r', encoding='utf-8') as f:
            h = json.load(f)
        if isinstance(h.get('entries'), list):
            return h
    except Exception:
        pass
    return {'entries': []}


def save_history(h):
    tmp = HISTORY_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(h, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HISTORY_PATH)


def entry_for(h, date_iso):
    for e in h['entries']:
        if e.get('date') == date_iso:
            return e
    return None


# ---------- settlement (ESPN scoreboards) ----------
# sportsbook.fetch_espn_board hits https://site.api.espn.com, which 403s
# from some networks (2026-09-16: blocked from both the PC and the cloud
# VM). Same data, alternate hosts — sportsbook.py now carries the same fallback.
_ESPN_URLS = (
    "https://site.api.espn.com/apis/site/v2/sports/%s/scoreboard",
    "https://site.web.api.espn.com/apis/site/v2/sports/%s/scoreboard",
    "http://site.api.espn.com/apis/site/v2/sports/%s/scoreboard",
)


def _parse_espn_board(data):
    games = []
    for ev in data.get("events", []) or []:
        comp = (ev.get("competitions") or [{}])[0]
        state = ((ev.get("status") or {}).get("type") or {}).get("state", "pre")
        home = away = None
        hs = aws = 0
        for c in comp.get("competitors", []) or []:
            team = (c.get("team") or {}).get("displayName", "")
            try:
                score = int(c.get("score") or 0)
            except (TypeError, ValueError):
                score = 0
            if c.get("homeAway") == "home":
                home, hs = team, score
            else:
                away, aws = team, score
        games.append({"home": home, "away": away, "state": state,
                      "home_score": hs, "away_score": aws,
                      "date": ev.get("date", "")})
    return games


def _fetch_espn_board(sport):
    espn = sportsbook.SPORTS[sport]["espn"]
    last_err = Exception("no ESPN host reachable")
    for tmpl in _ESPN_URLS:
        try:
            return _parse_espn_board(
                sportsbook._http_get_json(tmpl % espn))
        except Exception as e:
            last_err = e
    raise last_err


def _final_str(leg, game):
    hs, aws = game['home_score'], game['away_score']
    return '%s %d, %s %d' % (leg['home'], hs, leg['away'], aws)


def analyze_leg(leg, game):
    """One-line note grounded ONLY in the final score. No invented narrative."""
    hs, aws = game['home_score'], game['away_score']
    mine = hs if leg['pick'] == 'home' else aws
    theirs = aws if leg['pick'] == 'home' else hs
    team = leg['home'] if leg['pick'] == 'home' else leg['away']
    score = '%s %d-%d' % (team, mine, theirs)
    m = leg['market']
    if m == 'ml':
        if mine > theirs:
            return '%s — took it outright, ML cashes.' % score
        return '%s — upset, ML busts.' % score
    if m == 'spread':
        line = leg['line'] or 0
        line_s = ('+%g' % line) if line > 0 else ('%g' % line)
        if mine + line > theirs:
            return '%s (%s) — covered.' % (score, line_s)
        if mine + line == theirs:
            return '%s (%s) — pushed the number exactly.' % (score, line_s)
        return '%s (%s) — did not cover.' % (score, line_s)
    # over / under
    total = hs + aws
    line = leg['line'] or 0
    ou = 'Over' if leg['pick'] == 'over' else 'Under'
    if (leg['pick'] == 'over' and total > line) or \
            (leg['pick'] == 'under' and total < line):
        return '%d total (%s %g) — %s hits.' % (total, ou, line, ou)
    if total == line:
        return '%d total (%s %g) — landed on the number, push.' % (total, ou, line)
    return '%d total (%s %g) — %s misses.' % (total, ou, line, ou)


def _leg_voidable(leg, now):
    """A leg that commenced >72h ago is void if still ungradeable."""
    try:
        ct = datetime.fromisoformat(
            (leg.get('commence') or '').replace('Z', '+00:00'))
    except Exception:
        return False
    return now - ct > timedelta(hours=72)


def grade_history(h, boards=None, now=None):
    """Grade every entry with pending legs against ESPN boards.

    Idempotent: fully-final entries are never touched, so a day is never
    double-counted. Returns True if anything changed."""
    now = now or datetime.now(timezone.utc)
    fetched = {}
    changed = False

    def board_for(sport_key):
        if boards is not None:
            return boards.get(sport_key, [])
        if sport_key not in fetched:
            try:
                fetched[sport_key] = _fetch_espn_board(sport_key)
            except Exception:
                fetched[sport_key] = []
        return fetched[sport_key]

    for entry in h['entries']:
        entry_changed = False
        for ticket in entry.get('tickets', []):
            for leg in ticket.get('legs', []):
                if leg.get('leg_result') != 'pending':
                    continue
                game = sportsbook.find_espn_game(
                    board_for(leg['sport']), leg['home'], leg['away'],
                    leg.get('commence'))
                if game is None or game.get('state') != 'post':
                    if _leg_voidable(leg, now):
                        leg['leg_result'] = 'void'
                        leg['analysis'] = 'No final found — voided.'
                        entry_changed = True
                    continue
                leg['final'] = _final_str(leg, game)
                leg['leg_result'] = sportsbook.settle_leg(leg, game)
                leg['analysis'] = analyze_leg(leg, game)
                entry_changed = True
            ticket['result'] = _ticket_result(ticket)
        if entry_changed:
            changed = True
    if changed:
        _recompute_records(h)
    return changed


def _ticket_result(ticket):
    results = [l.get('leg_result') for l in ticket.get('legs', [])]
    if any(r == 'loss' for r in results):
        return 'bust'
    if any(r == 'pending' for r in results):
        return 'pending'
    if any(r == 'win' for r in results):
        return 'win'
    if any(r == 'push' for r in results):
        return 'push'
    return 'void'


def _recompute_records(h):
    """Deterministic running record across entries in date order."""
    wins = losses = net = 0
    for entry in sorted(h['entries'], key=lambda e: e.get('date', '')):
        for ticket in entry.get('tickets', []):
            res = ticket.get('result')
            stake = int(ticket.get('stake_budz') or 0)
            if res == 'win':
                winners = [l for l in ticket['legs']
                           if l.get('leg_result') == 'win']
                profit = int(stake * sportsbook.parlay_decimal(
                    [{'odds': l['odds']} for l in winners])) - stake
                wins += 1
                net += profit
            elif res == 'bust':
                losses += 1
                net -= stake
        entry['record'] = {'wins': wins, 'losses': losses, 'net_budz': net}


def seed_legacy(h):
    """One-time import of the old-format sheet (2026-09-16) so it gets graded
    and shows up in the first recap. Only runs when the history file is new."""
    try:
        with open(os.path.join(SOCIAL, 'budz_picks.json'), 'r',
                  encoding='utf-8') as f:
            old = json.load(f)
    except Exception:
        return False
    if 'tickets' in old or not old.get('parlay'):
        return False  # already new-format or nothing to import
    by_desc = {}
    for p in old.get('picks', []):
        # matchup "Detroit Lions at Buffalo Bills", pick "Buffalo Bills ML"
        parts = (p.get('matchup') or '').split(' at ')
        if len(parts) != 2:
            continue
        away, home = parts
        team = (p.get('pick') or '').rsplit(' ML', 1)[0]
        side = 'home' if team == home else ('away' if team == away else None)
        if not side:
            continue
        sport_key = LABEL_TO_KEY.get(p.get('sport', '').upper(), 'mlb')
        leg = {
            'sport': sport_key, 'home': home, 'away': away,
            'pick': side, 'market': 'ml', 'line': None,
            'odds': int(p.get('odds', '0').replace('+', '')),
            'desc': '%s ML (%s)' % (team, p.get('odds')),
            'tip': p.get('tip'), 'commence': None,
            'final': None, 'leg_result': 'pending', 'analysis': None,
        }
        by_desc[p.get('pick')] = leg
    legs = []
    for desc in old['parlay'].get('legs', []):
        # "Bills ML (-220)" -> match the pick carrying the same odds.
        import re as _re
        m = _re.search(r'\(([+-]?\d+)\)', desc)
        odds = m.group(1) if m else None
        matched = None
        for pick, leg in by_desc.items():
            if odds and str(leg['odds']) == odds.lstrip('+'):
                matched = leg
                break
        if matched is None:
            # fallback: a distinctive team word from the pick appears in desc
            for pick, leg in by_desc.items():
                words = [w for w in pick.replace(' ML', '').split()
                         if len(w) > 2]
                if any(w in desc for w in words):
                    matched = leg
                    break
        if matched and matched not in legs:
            legs.append(matched)
    if not legs:
        return False
    entry = {
        'date': '2026-09-16',
        'format': 'legacy',
        'tickets': [{
            'name': 'Legacy parlay',
            'tagline': 'Old-format ticket — carried over for grading.',
            'legs': legs,
            'combined_odds': old['parlay'].get('combined_odds'),
            'stake_budz': int(old['parlay'].get('stake_budz') or SAFE_STAKE),
            'result': 'pending',
        }],
        'record': {'wins': 0, 'losses': 0, 'net_budz': 0},
    }
    h['entries'].append(entry)
    return True


# ---------------- sheet ----------------

def build_recap(h, today_iso):
    """Yesterday's graded entry for the top of the sheet. None on day one."""
    try:
        y = (datetime.fromisoformat(today_iso)
             - timedelta(days=1)).date().isoformat()
    except Exception:
        return None
    entry = entry_for(h, y)
    if not entry or not entry.get('tickets'):
        return None
    return {
        'date': y,
        'tickets': entry['tickets'],
        'record': entry.get('record', {'wins': 0, 'losses': 0, 'net_budz': 0}),
    }


def build_sheet(boards=None, raw_games=None):
    for _k, _v in sportsbook.SPORTS.items():
        SPORT_NAMES[_k] = _v.get('label', _k.upper())

    today_iso = datetime.now(ET).date().isoformat()
    h = load_history()
    if not h['entries']:
        seed_legacy(h)
    if not entry_for(h, today_iso):
        raw = raw_games if raw_games is not None else sportsbook.get_all_games() or {}
        tickets = build_tickets(gather_candidates(raw))
        h['entries'].append({
            'date': today_iso, 'format': 'v2', 'tickets': tickets,
            'record': {'wins': 0, 'losses': 0, 'net_budz': 0},
        })
    grade_history(h, boards=boards)
    _recompute_records(h)  # carry the record forward even on quiet days
    save_history(h)

    entry = entry_for(h, today_iso)
    recap = build_recap(h, today_iso)
    today = datetime.now(ET).strftime('%A, %B %d, %Y')
    tickets = entry.get('tickets', []) if entry else []
    if tickets:
        note = ('Two same-day parlay tickets — every leg plays today. '
                'Leans, not locks. Play-money BUDZ only.')
    else:
        note = ('Slate too thin for same-day tickets today — the sheet will '
                'be back when games are. Leans, not locks. Play-money BUDZ only.')
    return {
        'date': today,
        'date_iso': today_iso,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'recap': recap,
        'tickets': tickets,
        'note': note,
    }


def compose_feed_body(sheet):
    parts = []
    if sheet['recap']:
        r = sheet['recap']
        bits = []
        for t in r['tickets']:
            bits.append('%s %s %s' % (
                RESULT_EMOJI.get(t.get('result'), '⏳'), t.get('name'),
                t.get('result', '').upper()))
        rec = r.get('record', {})
        parts.append('Yesterday: %s · Record %d-%d, %+d BUDZ' % (
            ' / '.join(bits), rec.get('wins', 0), rec.get('losses', 0),
            rec.get('net_budz', 0)))
    for t in sheet['tickets']:
        legs = ' / '.join(l['desc'] for l in t['legs'][:4])
        parts.append('%s (%s): %s — %d BUDZ' % (
            t['name'], t['combined_odds'], legs, t['stake_budz']))
    body = "🎯 Budz Picks — %s. %s Leans not locks, play money. /picks" % (
        sheet['date'].split(',')[0], '. '.join(parts))
    if len(body) > 600:
        cut = body.rfind(' / ', 0, 599)
        body = (body[:cut] if cut > 40 else body[:599]) + '…'
    return body


def post_to_feed(sheet):
    if not sheet['tickets'] and not sheet['recap']:
        return
    body = compose_feed_body(sheet)
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
            " AND substr(created_at, 1, 10) = ? AND body LIKE '%Budz Picks — %'",
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
    print('wrote %s (%d tickets)' % (out, len(sheet['tickets'])))
    post_to_feed(sheet)


if __name__ == '__main__':
    main()
