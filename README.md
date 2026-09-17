# 🌿 The Higher Club

A companion social network for the **T.H.C.** stream community — Brad's Twitch
AI co-host + chat casino + grow monitor project. Web-first (no mobile apps),
so the cannabis-friendly theme never has to fight app-store policies.

**V1 is a working scaffold:** accounts, feed, follows, DMs, casino
leaderboard, Budz Book lines, grow journal, avatar uploads with mod approval,
and a stream-sync API so the stream-PC bot can pull approved avatars for
on-stream overlays.

## Run it

```bash
cd thc-social
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# seed demo users + posts (admin / admin123)
python -m app.seed

# start the server
uvicorn app.main:app
```

Open http://127.0.0.1:8000 — you'll land on the welcome page. Log in as
**admin / admin123** to try the mod tools (avatar approvals + reports).

Run the tests:

```bash
pytest -q
```

## What's real vs mocked

| Feature | Status |
|---|---|
| Register / login / logout (bcrypt + signed cookies) | ✅ real |
| Feed, likes, comments, delete-own-post | ✅ real |
| Follow / unfollow, follower counts | ✅ real |
| DMs (1:1, 5-second polling — no websockets in v1) | ✅ real |
| Avatar upload (square-crop, 512px max, JPG/PNG) | ✅ real |
| Avatar mod approval queue | ✅ real |
| `GET /api/stream/avatars` (approved avatars only) | ✅ real |
| Post reporting + admin delete/dismiss | ✅ real |
| Casino leaderboard | ⚠️ **mock data** — see `app/thc_adapter.py` for how to wire the real casino DB |
| Budz Book sportsbook lines | ⚠️ **mock data** — adapter ready for `sportsbook.get_all_games()` |
| Grow tent stats | ⚠️ **mock data** — adapter ready for real sensor readings |
| Grow journal posts (user photos) | ✅ real |

The rule: **routes never touch data sources directly** — they call
`app/thc_adapter.py`. Swapping mock → live is a one-file change.

## Stream avatar sync contract

The stream-PC bot polls for overlay-ready avatars (entry animations, fan-flair
celebrations):

- **Poll:** `GET {BASE}/api/stream/avatars` → `{"queenshida": "https://…/media/avatars/queenshida.jpg", …}`
  - Only **moderator-approved** avatars are listed. Unapproved uploads are invisible here.
  - `?key=twitch` keys the map by `twitch_username` (falls back to site username)
    so the bot can match chatters directly: `GET /api/stream/avatars?key=twitch`
- **Fetch one:** `GET {BASE}/api/stream/avatar/<username>` → JPEG file (404 unless approved)
- **Bot behavior:** poll every 5 minutes, cache files locally on the stream PC,
  only re-download URLs it hasn't seen. Map **Twitch username → avatar file**.
- Users set their Twitch username on the Settings page; mods approve avatars at
  `/admin` (approve = stream-eligible, reject = file deleted).

Avatar rules: square-cropped server-side, max 512px, stored at
`media/avatars/<username>.jpg`. Re-uploads reset approval (a changed face
needs a fresh mod OK).

## Project layout

```
thc-social/
  app/
    main.py          # all routes (one readable file, top to bottom)
    db.py            # SQLite schema + per-request connections
    auth.py          # bcrypt hashing + signed cookie sessions
    thc_adapter.py   # mock/live bridge to the real T.H.C. systems
    seed.py          # demo users, posts, follows, DMs
    templates/       # Jinja2 pages (no JS framework)
    static/          # style.css + tiny vanilla app.js
  media/avatars/     # uploaded avatars (created at runtime)
  media/posts/       # uploaded post images (created at runtime)
  tests/test_app.py  # pytest suite (isolated throwaway DB)
```

## Security notes (v1)

- `SESSION_SECRET` in `app/auth.py` is a hard-coded **dev** constant — fine for
  local use, must become an env var before this is public.
- No `.env` needed for v1. Never commit real secrets.
- Uploads are type/size-checked; avatar filenames are derived from the
  validated username (no path traversal).

## Roadmap (not v1)

- Avatar **builder** (pick-a-character creator) — v1 only does photo uploads
- Real-time websockets for DMs/feed
- Notifications (mentions, likes, follows)
- Video uploads (images only in v1)
- Twitch OAuth login (username field for now)
- Full-text search, hashtags, trending
- Mobile apps — intentionally not planned; web-first dodges app-store issues
  with cannabis content
