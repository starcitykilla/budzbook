"""BudzBook — companion social network for the T.H.C. stream community.

Run:  pip install -r requirements.txt
       uvicorn app.main:app        (from the project root)

Route map (kept in one file on purpose so it's easy to read top to bottom):
  Public:      /  /register  /login  /logout  /age-check  /terms
               /manifest.webmanifest  (PWA install manifest)
               /api/stream/avatars  /api/stream/avatar/<username>   (stream-PC sync)
  Social:      /feed  /post  /post/<id>/like  /post/<id>/comment
               /post/<id>/delete  /post/<id>/report
               /u/<username>  /u/<username>/followers|following
               /settings (profile + avatar upload)
  DMs:         /messages  /messages/<username>  (polling, no websockets in v1)
  THC tie-ins: /leaderboard  /sportsbook  /grow
  Admin:       /admin  (reports + avatar approvals)

Age gate: everything except /age-check, /static, /media and /api/stream/*
requires a signed 21+ cookie (see the age_gate middleware below).
"""
import base64
import json
import math
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone, timedelta

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import aiosqlite
import httpx
import io
from PIL import Image, ImageOps

from . import thc_adapter
from . import currency
from . import ticker
from .auth import (AGE_COOKIE, SESSION_COOKIE, hash_password, make_age_token,
                   make_pending_oauth_token, make_session_token,
                   read_age_token, read_pending_oauth_token,
                   read_session_token, verify_password)
from .db import (AVATAR_DIR, BANNER_DIR, COMMENT_IMG_DIR, DB_PATH, FIGURINE_DIR,
                 MEDIA_DIR, POST_IMG_DIR, avatar_exists, avatar_path_for,
                 figurine_exists, get_db, init_db)
from . import twitch_oauth
from .twitch_oauth import (authorize_url, configured as twitch_configured,
                           exchange_code, fetch_twitch_user, make_state,
                           read_state)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
POST_MAX_LEN = 1000
FEED_PAGE_SIZE = 10

# Terms of Service clickwrap version. Bumping this forces every user
# through /terms again (middleware compares users.terms_version).
TERMS_VERSION = "2026-09-17"

# Twitch channel embedded on the /watch page.
TWITCH_CHANNEL = "krzy_budz"
# Domains allowed as embed parents (Twitch requirement). The request host
# is appended at runtime so local/tunnel access works too.
TWITCH_EMBED_PARENTS = ("budzbook.us", "www.budzbook.us", "localhost")
# Followed-streams grid on /watch: channel logins shown as cards, in order.
# Brad's followed accounts live here; clicking a card opens that channel's
# player view. Override with WATCH_CHANNELS="krzy_budz,queenshida"
# (comma-separated, case-insensitive).
_watch_env = (os.environ.get("WATCH_CHANNELS") or "").strip()
WATCH_CHANNELS = tuple(c for c in
                       (x.strip().lower() for x in _watch_env.split(","))
                       if c) or (TWITCH_CHANNEL,)
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# Public Twitch OAuth callback (set when serving behind the tunnel;
# falls back to the request URL when unset).
TWITCH_REDIRECT_URI = (os.environ.get("TWITCH_REDIRECT_URI") or "").strip() or None


def twitch_redirect_uri(request):
    if TWITCH_REDIRECT_URI:
        return TWITCH_REDIRECT_URI
    return str(request.url_for("twitch_callback"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="BudzBook", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "app", "static")), name="static")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
HASHTAG_RE = re.compile(r"#(\w+)")
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v")
MAX_VIDEO_BYTES = 50 * 1024 * 1024


def extract_hashtags(body: str):
    """Lowercased hashtag set from a post body."""
    return {t.lower() for t in HASHTAG_RE.findall(body or "")}


def linkify_tags(body: str):
    """Escape HTML, then linkify #hashtags. Returns Markup (safe)."""
    from markupsafe import escape, Markup
    parts, last = [], 0
    for m in HASHTAG_RE.finditer(body or ""):
        parts.append(escape(body[last:m.start()]))
        tag = m.group(1)
        parts.append(
            f'<a class="htag" href="/tag/{tag.lower()}">#{escape(tag)}</a>')
        last = m.end()
    parts.append(escape(body[last:]))
    return Markup("".join(parts))


async def _save_hashtags(db, post_id: int, body: str):
    for tag in extract_hashtags(body):
        await db.execute(
            "INSERT OR IGNORE INTO post_hashtags (post_id, tag) VALUES (?, ?)",
            (post_id, tag))


templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "app", "templates"))
templates.env.filters["linkify"] = linkify_tags


def _fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%b %d, %I:%M %p")
    except Exception:
        return iso


templates.env.filters["fdt"] = _fmt_dt


# --------------------------------------------------------------------------
# 21+ age gate — BudzBook is a cannabis community.
# Everything except the gate page itself, static/media assets, and the
# --------------------------------------------------------------------------
# Terms of Service clickwrap enforcement.
# No authenticated user may use the site without accepting the current
# TERMS_VERSION. Unaccepted users are bounced to /terms (with ?next= so
# they land back where they were going after accepting).
# Registered before the age gate so the age check runs first.
# --------------------------------------------------------------------------
def _terms_exempt(path: str) -> bool:
    return (
        path in ("/terms", "/terms/accept", "/age-check", "/login",
                 "/logout", "/register", "/manifest.webmanifest")
        or path.startswith(("/static/", "/media/", "/api/", "/auth/"))
    )


def _terms_accepted(row) -> bool:
    """True when the user row shows acceptance of the current terms."""
    try:
        accepted_at = row["terms_accepted_at"]
        version = row["terms_version"]
    except (KeyError, TypeError, IndexError):
        return False
    return bool(accepted_at) and version == TERMS_VERSION


def _safe_next(value: str) -> str:
    """Allow only internal redirect targets (blocks open redirects)."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return ""


@app.middleware("http")
async def terms_gate(request: Request, call_next):
    if not _terms_exempt(request.url.path):
        uid = read_session_token(request.cookies.get(SESSION_COOKIE, ""))
        if isinstance(uid, int):
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT terms_accepted_at, terms_version FROM users WHERE id = ?",
                    (uid,))
                row = await cur.fetchone()
            if row is None or not _terms_accepted(row):
                nxt = request.url.path
                if request.url.query:
                    nxt += "?" + request.url.query
                dest = "/terms?next=" + urllib.parse.quote(nxt, safe="")
                return RedirectResponse(dest, status_code=303)
    return await call_next(request)


# stream-PC sync API requires a signed age cookie.
# --------------------------------------------------------------------------
def _age_exempt(path: str) -> bool:
    return (
        path in ("/age-check", "/manifest.webmanifest", "/ticker", "/ticker.json", "/livelook", "/livelook.json")
        or path.startswith(("/static/", "/media/", "/api/stream/"))
    )


@app.middleware("http")
async def age_gate(request: Request, call_next):
    if not _age_exempt(request.url.path):
        if not read_age_token(request.cookies.get(AGE_COOKIE, "")):
            return RedirectResponse("/age-check", status_code=303)
    return await call_next(request)


@app.get("/age-check")
async def age_check_form(request: Request):
    if read_age_token(request.cookies.get(AGE_COOKIE, "")):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "age_check.html", {"user": None})


@app.post("/age-check")
async def age_check_submit(request: Request,
                           month: str = Form(...), day: str = Form(...),
                           year: str = Form(...)):
    def denied_page():
        return templates.TemplateResponse(
            request, "age_check.html", {"user": None, "denied": True},
            status_code=403)

    try:
        dob = date(int(year), int(month), int(day))
    except ValueError:
        return templates.TemplateResponse(
            request, "age_check.html",
            {"user": None, "error": "That date doesn't look right — try again."},
            status_code=400)
    if dob > date.today():
        return templates.TemplateResponse(
            request, "age_check.html",
            {"user": None, "error": "Birth date can't be in the future."},
            status_code=400)
    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if age < 21:
        return denied_page()
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(AGE_COOKIE, make_age_token(),
                    max_age=365 * 24 * 3600, httponly=True, samesite="lax")
    return resp


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------
async def current_user(request: Request, db=Depends(get_db)):
    """Return the logged-in user row, or None. Never raises."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    uid = read_session_token(token)
    if not uid:
        return None
    cur = await db.execute("SELECT * FROM users WHERE id = ?", (uid,))
    return await cur.fetchone()


def login_required(user):
    if user is None:
        return RedirectResponse("/login?msg=Please+log+in+first", status_code=303)
    return None


def admin_required(user):
    if user is None or not user["is_admin"]:
        return RedirectResponse("/?msg=Admins+only", status_code=303)
    return None


def _now():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Double-submit protection: a double-tapped Post/Comment/Send button must
# not create two rows. An identical write from the same user inside
# DEDUP_WINDOW_S is treated as a retry of the first one and skipped.
# --------------------------------------------------------------------------
DEDUP_WINDOW_S = 30


def _dedup_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=DEDUP_WINDOW_S)).isoformat()


async def _recent_duplicate(db, table: str, where: str, args: tuple) -> bool:
    """True when the same user already made this exact write recently."""
    cur = await db.execute(
        f"SELECT 1 FROM {table} WHERE {where} AND created_at >= ? LIMIT 1",
        (*args, _dedup_cutoff()))
    return bool(await cur.fetchone())


# --------------------------------------------------------------------------
# Avatars (upload -> mod approval -> stream sync)
# --------------------------------------------------------------------------
def process_avatar_upload(upload: UploadFile, username: str) -> None:
    """Square-crop to max 512px and save as media/avatars/<username>.jpg.

    Raises ValueError on bad input. The avatar is NOT stream-eligible until
    a moderator approves it (avatar_approved = 1).
    """
    if upload.content_type not in ("image/jpeg", "image/png"):
        raise ValueError("Avatar must be a JPG or PNG image.")
    data = upload.file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Image is too large (5 MB max).")
    try:
        img = Image.open(__import__("io").BytesIO(data)).convert("RGB")
    except Exception:
        raise ValueError("Could not read that image file.")
    w, h = img.size
    side = min(w, h)
    img = img.crop(((w - side) // 2, (h - side) // 2,
                    (w + side) // 2, (h + side) // 2))
    img.thumbnail((512, 512), Image.LANCZOS)
    img.save(avatar_path_for(username), "JPEG", quality=88)


def process_banner_upload(upload: UploadFile, username: str) -> str:
    """Center-crop to 4:1 (max 1200x300) and save as media/banners/<username>.jpg.

    Returns the media-relative path. Raises ValueError on bad input.
    """
    if upload.content_type not in ("image/jpeg", "image/png"):
        raise ValueError("Banner must be a JPG or PNG image.")
    data = upload.file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Image is too large (5 MB max).")
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        raise ValueError("Could not read that image file.")
    w, h = img.size
    ratio = 4.0
    if w / h > ratio:
        nw = int(h * ratio)
        x0 = (w - nw) // 2
        img = img.crop((x0, 0, x0 + nw, h))
    else:
        nh = int(w / ratio)
        y0 = (h - nh) // 2
        img = img.crop((0, y0, w, y0 + nh))
    img.thumbnail((1200, 300), Image.LANCZOS)
    img.save(os.path.join(BANNER_DIR, f"{username}.jpg"), "JPEG", quality=88)
    return f"banners/{username}.jpg"


async def mirror_twitch_avatar(username: str, avatar_url: str) -> bool:
    # Mirror the Twitch profile image to media/avatars/<username>.jpg so it
    # shows on the profile and stream. Auto-approved: verified Twitch identity.
    # Best effort: returns False instead of raising.
    if not avatar_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(avatar_url)
            resp.raise_for_status()
            data = resp.content
        if len(data) > MAX_UPLOAD_BYTES:
            return False
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        img.thumbnail((512, 512), Image.LANCZOS)
        img.save(avatar_path_for(username), "JPEG", quality=88)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Public pages
# --------------------------------------------------------------------------
@app.get("/")
async def index(request: Request, user=Depends(current_user), msg: str = ""):
    if user:
        return RedirectResponse("/feed", status_code=303)
    return templates.TemplateResponse(request, "index.html", { "user": user, "msg": msg})


# --------------------------------------------------------------------------
# Terms of Service (clickwrap). /terms is public — everyone must be able to
# read the Terms before joining. /terms/accept records acceptance for
# logged-in users, or finishes creating a staged Twitch OAuth account.
# --------------------------------------------------------------------------
@app.get("/terms")
async def terms_page(request: Request, user=Depends(current_user),
                     pending: str = "", next: str = "", msg: str = ""):
    pending_profile = read_pending_oauth_token(pending) if pending else None
    show_accept = bool(pending_profile) or (user is not None and not _terms_accepted(user))
    return templates.TemplateResponse(request, "terms.html", {
        "user": user,
        "msg": msg,
        "show_accept": show_accept,
        "pending": pending if pending_profile else "",
        "pending_name": (pending_profile or {}).get("display") or "",
        "next": _safe_next(next),
        "error": "",
    })


@app.post("/terms/accept")
async def terms_accept(request: Request, db=Depends(get_db),
                       user=Depends(current_user),
                       agree: str = Form(""), pending: str = Form(""),
                       next: str = Form("")):
    nxt = _safe_next(next) or "/feed"
    if agree != "yes":
        # Server-side enforcement: the checkbox is mandatory.
        pending_profile = read_pending_oauth_token(pending) if pending else None
        return templates.TemplateResponse(request, "terms.html", {
            "user": user,
            "msg": "",
            "show_accept": True,
            "pending": pending if pending_profile else "",
            "pending_name": (pending_profile or {}).get("display") or "",
            "next": nxt if nxt != "/feed" else "",
            "error": "You must check the box to accept the Terms of Service.",
        }, status_code=400)
    pending_profile = read_pending_oauth_token(pending) if pending else None
    if pending_profile:
        # Staged Twitch OAuth signup: create the account now that the
        # Terms are accepted. Re-check username uniqueness at accept time.
        login = pending_profile["login"]
        username = login
        n = 0
        while True:
            cur = await db.execute("SELECT id FROM users WHERE username = ?",
                                   (username,))
            if not await cur.fetchone():
                break
            n += 1
            username = f"{login}_{n}"
        # Same race guard as the old callback path: Twitch ID taken meanwhile?
        cur = await db.execute("SELECT id FROM users WHERE twitch_id = ?",
                               (pending_profile["twitch_id"],))
        if await cur.fetchone():
            return RedirectResponse("/login?msg=That+Twitch+account+is+already+linked",
                                    status_code=303)
        now = _now()
        await db.execute(
            "INSERT INTO users (username, display_name, password_hash, bio, " +
            "twitch_username, twitch_id, twitch_avatar, twitch_verified, " +
            "terms_accepted_at, terms_version, created_at)" +
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (username, pending_profile["display"],
             hash_password("twitch-oauth:" + secrets.token_hex(16)),
             pending_profile["bio"], login, pending_profile["twitch_id"],
             pending_profile["avatar"], now, TERMS_VERSION, now))
        await db.commit()
        cur = await db.execute("SELECT * FROM users WHERE twitch_id = ?",
                               (pending_profile["twitch_id"],))
        new_user = await cur.fetchone()
        if pending_profile["avatar"]:
            if await mirror_twitch_avatar(username, pending_profile["avatar"]):
                await db.execute("UPDATE users SET avatar_approved = 1 WHERE id = ?",
                                 (new_user["id"],))
                await db.commit()
        await currency.grant_welcome_bonus(db, new_user["id"])
        resp = RedirectResponse(
            nxt + "?msg=Welcome+to+BudzBook!+100+Budz+on+the+house." if nxt == "/feed"
            else nxt, status_code=303)
        resp.set_cookie(SESSION_COOKIE, make_session_token(new_user["id"]),
                        httponly=True, samesite="lax")
        return resp
    if user is None:
        return RedirectResponse("/login?msg=Please+log+in+first", status_code=303)
    await db.execute(
        "UPDATE users SET terms_accepted_at = ?, terms_version = ? WHERE id = ?",
        (_now(), TERMS_VERSION, user["id"]))
    await db.commit()
    return RedirectResponse(nxt, status_code=303)


@app.get("/register")
async def register_form(request: Request, user=Depends(current_user), msg: str = ""):
    if user:
        return RedirectResponse("/feed", status_code=303)
    return templates.TemplateResponse(request, "register.html", { "user": None, "msg": msg,
        "twitch_configured": twitch_configured()})


@app.post("/register")
async def register(request: Request, db=Depends(get_db),
                   username: str = Form(...), display_name: str = Form(""),
                   password: str = Form(...), twitch_username: str = Form(""), agree_terms: str = Form("")):
    username = username.strip()
    msg = None
    if not USERNAME_RE.match(username):
        msg = "Username must be 3-20 chars: letters, numbers, underscores."
    elif len(password) < 6:
        msg = "Password must be at least 6 characters."
    elif agree_terms != "yes":
        msg = "You must agree to the Terms of Service to join BudzBook."
    else:
        cur = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
        if await cur.fetchone():
            msg = "That username is taken."
    if not msg:
        try:
            now = _now()
            await db.execute(
                "INSERT INTO users (username, display_name, password_hash, twitch_username, "
                "terms_accepted_at, terms_version, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (username, display_name.strip() or username, hash_password(password),
                 twitch_username.strip(), now, TERMS_VERSION, now))
            await db.commit()
        except sqlite3.IntegrityError:
            # Raced double-submit: the first tap already took this username.
            await db.rollback()
            msg = "That username is taken."
    if msg:
        return templates.TemplateResponse(request, "register.html", { "user": None, "msg": msg,
            "twitch_configured": twitch_configured()},
                                          status_code=400)
    cur = await db.execute("SELECT * FROM users WHERE username = ?", (username,))
    user = await cur.fetchone()
    await currency.grant_welcome_bonus(db, user["id"])
    resp = RedirectResponse("/feed?msg=Welcome+to+BudzBook!+100+Budz+on+the+house.",
                            status_code=303)
    resp.set_cookie(SESSION_COOKIE, make_session_token(user["id"]), httponly=True, samesite="lax")
    return resp


@app.get("/login")
async def login_form(request: Request, user=Depends(current_user), msg: str = ""):
    if user:
        return RedirectResponse("/feed", status_code=303)
    return templates.TemplateResponse(request, "login.html", { "user": None, "msg": msg,
        "twitch_configured": twitch_configured()})


@app.post("/login")
async def login(request: Request, db=Depends(get_db),
                username: str = Form(...), password: str = Form(...)):
    cur = await db.execute("SELECT * FROM users WHERE username = ?", (username.strip(),))
    user = await cur.fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(request, "login.html", { "user": None,
                                           "msg": "Wrong username or password.",
                                           "twitch_configured": twitch_configured()},
                                          status_code=400)
    resp = RedirectResponse("/feed", status_code=303)
    resp.set_cookie(SESSION_COOKIE, make_session_token(user["id"]), httponly=True, samesite="lax")
    return resp


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/?msg=Logged+out", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# --------------------------------------------------------------------------
# Twitch OAuth login ("Continue with Twitch")
# --------------------------------------------------------------------------
@app.get("/auth/twitch")
async def twitch_login(request: Request, user=Depends(current_user)):
    """Start the Twitch OAuth flow. Logged-in users link; logged-out users log in."""
    if not twitch_configured():
        return RedirectResponse("/login?msg=Twitch+login+isn't+set+up+yet",
                                status_code=303)
    redirect_uri = twitch_redirect_uri(request)
    state = make_state(link_uid=user["id"] if user else None)
    return RedirectResponse(authorize_url(redirect_uri, state), status_code=303)


@app.get("/auth/twitch/callback", name="twitch_callback")
async def twitch_callback(request: Request, db=Depends(get_db),
                          code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse("/login?msg=Twitch+login+was+cancelled",
                                status_code=303)
    st = read_state(state) if state else None
    if not st or not code or not twitch_configured():
        return RedirectResponse("/login?msg=Twitch+login+failed", status_code=303)
    redirect_uri = twitch_redirect_uri(request)
    try:
        token = await exchange_code(code, redirect_uri)
        tw = await fetch_twitch_user(token)
    except Exception:
        return RedirectResponse("/login?msg=Twitch+login+failed,+try+again",
                                status_code=303)
    twitch_id, login = str(tw["id"]), tw["login"]
    display = tw.get("display_name") or login
    avatar = tw.get("profile_image_url") or ""
    tw_bio = (tw.get("description") or "").strip()

    # Link flow: user was already logged in — attach Twitch to their account.
    if st.get("link_uid"):
        cur = await db.execute(
            "SELECT id FROM users WHERE twitch_id = ? AND id != ?",
            (twitch_id, st["link_uid"]))
        if await cur.fetchone():
            return RedirectResponse(
                "/settings?msg=That+Twitch+account+is+already+linked",
                status_code=303)
        await db.execute(
            "UPDATE users SET twitch_id = ?, twitch_username = ?, "
            "twitch_avatar = ?, twitch_verified = 1 WHERE id = ?",
            (twitch_id, login, avatar, st["link_uid"]))
        cur = await db.execute("SELECT username, bio FROM users WHERE id = ?",
                               (st["link_uid"],))
        link_user = await cur.fetchone()
        if tw_bio and not link_user["bio"]:
            await db.execute("UPDATE users SET bio = ? WHERE id = ?",
                             (tw_bio[:500], st["link_uid"]))
        if avatar and await mirror_twitch_avatar(link_user["username"], avatar):
            await db.execute("UPDATE users SET avatar_approved = 1 WHERE id = ?",
                             (st["link_uid"],))
        await db.commit()
        return RedirectResponse("/settings?msg=Twitch+account+linked",
                                status_code=303)

    # Login flow: find by verified Twitch ID, else create an account.
    cur = await db.execute("SELECT * FROM users WHERE twitch_id = ?",
                           (twitch_id,))
    user = await cur.fetchone()
    if not user:
        # NEW Twitch user: do NOT create the account yet. Stage the OAuth
        # profile in a signed token and send them to /terms — the account
        # is created by POST /terms/accept only after they agree ("no
        # non-agree joins").
        pending = make_pending_oauth_token({
            "twitch_id": twitch_id,
            "login": login,
            "display": display,
            "avatar": avatar,
            "bio": tw_bio[:500],
        })
        return RedirectResponse(
            "/terms?pending=" + urllib.parse.quote(pending, safe=""),
            status_code=303)
    else:
        await db.execute(
            "UPDATE users SET twitch_username = ?, twitch_avatar = ?, "
            "twitch_verified = 1 WHERE id = ?",
            (login, avatar, user["id"]))
        if tw_bio and not user["bio"]:
            await db.execute("UPDATE users SET bio = ? WHERE id = ?",
                             (tw_bio[:500], user["id"]))
        if avatar and not avatar_exists(user["username"]):
            if await mirror_twitch_avatar(user["username"], avatar):
                await db.execute("UPDATE users SET avatar_approved = 1 WHERE id = ?",
                                 (user["id"],))
        await db.commit()
    resp = RedirectResponse("/feed", status_code=303)
    resp.set_cookie(SESSION_COOKIE, make_session_token(user["id"]),
                    httponly=True, samesite="lax")
    return resp


# --------------------------------------------------------------------------
# PWA install support (BudzBook as an installable app — no app store needed)
# --------------------------------------------------------------------------
@app.get("/manifest.webmanifest")
async def pwa_manifest():
    """Serve the web app manifest with the correct MIME type so browsers
    offer 'Install app' / 'Add to Home Screen'."""
    return FileResponse(
        os.path.join(BASE_DIR, "app", "static", "manifest.webmanifest"),
        media_type="application/manifest+json")


# --------------------------------------------------------------------------
# Stream-sync API (polled by the stream-PC bot for on-stream overlays)
# --------------------------------------------------------------------------
@app.get("/api/stream/avatars")
async def stream_avatars(request: Request, db=Depends(get_db), key: str = "site"):
    """{username: avatar_url} for APPROVED avatars only.

    ?key=site   (default) keys by site username.
    ?key=twitch keys by twitch_username (falls back to site username when unset)
                so the bot can map chat chatters directly.
    URLs are absolute so the bot can fetch without knowing the base URL.
    Contract: the bot polls this every 5 minutes, caches locally, and only
    re-downloads files whose URL it hasn't seen before.
    """
    cur = await db.execute(
        "SELECT username, twitch_username FROM users WHERE avatar_approved = 1")
    base = str(request.base_url).rstrip("/")
    out = {}
    for row in await cur.fetchall():
        k = row["username"]
        if key == "twitch":
            k = row["twitch_username"] or row["username"]
        out[k] = f"{base}/media/avatars/{row['username']}.jpg"
    return JSONResponse(out)


@app.get("/api/stream/avatar/{username}")
async def stream_avatar(username: str, db=Depends(get_db)):
    """Serve one approved avatar file (404 unless approved and present)."""
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username or ""):
        return JSONResponse({"error": "bad username"}, status_code=404)
    cur = await db.execute(
        "SELECT avatar_approved FROM users WHERE username = ?", (username,))
    row = await cur.fetchone()
    if not row or not row["avatar_approved"] or not avatar_exists(username):
        return JSONResponse({"error": "no approved avatar"}, status_code=404)
    return FileResponse(avatar_path_for(username), media_type="image/jpeg")


# --------------------------------------------------------------------------
# Feed / posts
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Profile themes + feed prefs
# --------------------------------------------------------------------------
THEME_SWATCHES = ["#e8c547", "#3ddc84", "#a06cd5", "#ff5a5a",
                  "#4da3ff", "#ff9f43", "#ff6b9d", "#2dd4bf"]


def _feed_prefs(user) -> dict:
    """Parse the user's feed_prefs JSON into sort / reshares / muted words."""
    try:
        prefs = json.loads(user["feed_prefs"] or "{}")
    except Exception:
        prefs = {}
    sort = prefs.get("sort")
    words = [w.strip().lower() for w in str(prefs.get("muted_words", "")).split(",")]
    return {"sort": sort if sort in ("latest", "top") else "latest",
            "show_reshares": prefs.get("show_reshares", True),
            "muted_words": [w for w in words if w]}


def _tenor_key() -> str:
    """Tenor API key from env (set TENOR_API_KEY as a User env var on the PC)."""
    return os.environ.get("TENOR_API_KEY", "").strip()


async def _post_rows(db, me_id, where="", args=(), limit=FEED_PAGE_SIZE, offset=0,
                     order="latest"):
    order_sql = "p.created_at DESC" if order != "top" else "like_count DESC, p.created_at DESC"

    cur = await db.execute(
        f"""SELECT p.*, u.username, u.display_name, u.avatar_approved, u.equipped_frame,
                   g.name AS group_name,
                   (SELECT COUNT(*) FROM likes l WHERE l.post_id = p.id) AS like_count,
                   (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comment_count,
                   (SELECT COUNT(*) FROM likes l2 WHERE l2.post_id = p.id AND l2.user_id = ?) AS liked
            FROM posts p JOIN users u ON u.id = p.user_id
            LEFT JOIN groups g ON g.id = p.group_id
            {where}
            ORDER BY {order_sql} LIMIT ? OFFSET ?""",
        (me_id, *args, limit, offset))
    posts = [dict(r) for r in await cur.fetchall()]
    # Attach comments to each post (fine at v1 scale).
    for p in posts:
        cur = await db.execute(
            """SELECT c.*, u.username, u.display_name FROM comments c
               JOIN users u ON u.id = c.user_id
               WHERE c.post_id = ? ORDER BY c.created_at ASC""", (p["id"],))
        p["comments"] = [dict(r) for r in await cur.fetchall()]
    return posts


@app.get("/feed")
async def feed(request: Request, db=Depends(get_db), user=Depends(current_user),
               filter: str = "all", page: int = 1, msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    await maybe_post_golive(db)
    page = max(1, page)
    offset = (page - 1) * FEED_PAGE_SIZE
    if filter == "following":
        where = """WHERE p.user_id = ? OR p.user_id IN
                   (SELECT followed_id FROM follows WHERE follower_id = ?)"""
        args = (user["id"], user["id"])
    else:
        filter = "all"
        where, args = "", ()
    prefs = _feed_prefs(user)
    if not prefs["show_reshares"]:
        # No reshare post kind exists yet; this filter is a no-op today and
        # stays correct if one is ever added.
        where = (where + " AND p.kind != 'reshare'") if where else "WHERE p.kind != 'reshare'"
    posts = await _post_rows(db, user["id"], where, args, FEED_PAGE_SIZE + 1, offset,
                             order=prefs["sort"])
    muted = prefs["muted_words"]
    if muted:
        posts = [p for p in posts
                 if not any(w in (p["body"] or "").lower() for w in muted)]
    has_more = len(posts) > FEED_PAGE_SIZE
    return templates.TemplateResponse(request, "feed.html", { "user": user, "posts": posts[:FEED_PAGE_SIZE],
        "filter": filter, "page": page, "has_more": has_more, "msg": msg})


@app.post("/post")
async def create_post(request: Request, db=Depends(get_db), user=Depends(current_user),
                      body: str = Form(...), kind: str = Form("post"),
                      image: UploadFile = File(None), video: UploadFile = File(None),
                      group_id: int = Form(None)):
    redir = login_required(user)
    if redir:
        return redir
    body = body.strip()
    if not body:
        return RedirectResponse("/feed?msg=Post+can't+be+empty", status_code=303)
    if len(body) > POST_MAX_LEN:
        return RedirectResponse("/feed?msg=Posts+are+1000+chars+max", status_code=303)
    if kind not in ("post", "grow"):
        kind = "post"
    if await _recent_duplicate(
            db, "posts",
            "user_id = ? AND body = ? AND kind = ?"
            " AND COALESCE(group_id, -1) = COALESCE(?, -1)",
            (user["id"], body, kind, group_id)):
        # Double-tap: the first tap already posted this. Don't make a twin.
        return RedirectResponse("/feed?msg=Posted", status_code=303)
    image_path = None
    if image and image.filename:
        ext = os.path.splitext(image.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            return RedirectResponse("/feed?msg=Image+must+be+JPG/PNG/GIF/WebP", status_code=303)
        data = await image.read()
        if len(data) > MAX_UPLOAD_BYTES:
            return RedirectResponse("/feed?msg=Image+too+large+(5MB+max)", status_code=303)
        name = f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(POST_IMG_DIR, name), "wb") as f:
            f.write(data)
        image_path = f"posts/{name}"
    if group_id:
        cur = await db.execute("SELECT 1 FROM groups WHERE id = ?", (group_id,))
        if not await cur.fetchone():
            return RedirectResponse("/groups?msg=Group+not+found", status_code=303)
        cur = await db.execute(
            "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, user["id"]))
        if not await cur.fetchone():
            return RedirectResponse(
                f"/groups/{group_id}?msg=Join+the+group+to+post", status_code=303)
    video_path = None
    if video and video.filename:
        vext = os.path.splitext(video.filename)[1].lower()
        if vext not in VIDEO_EXTS:
            return RedirectResponse("/feed?msg=Video+must+be+MP4/MOV/WebM", status_code=303)
        vdata = await video.read()
        if len(vdata) > MAX_VIDEO_BYTES:
            return RedirectResponse("/feed?msg=Video+too+large+(50MB+max)", status_code=303)
        vname = f"{uuid.uuid4().hex}{vext}"
        with open(os.path.join(POST_IMG_DIR, vname), "wb") as f:
            f.write(vdata)
        video_path = f"posts/{vname}"
    cur = await db.execute(
        "INSERT INTO posts (user_id, body, image_path, video_path, group_id, kind, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user["id"], body, image_path, video_path, group_id, kind, _now()))
    post_id = cur.lastrowid
    await _save_hashtags(db, post_id, body)
    await db.commit()
    if group_id:
        dest = f"/groups/{group_id}?msg=Posted"
    else:
        dest = "/grow?msg=Grow+update+posted" if kind == "grow" else "/feed?msg=Posted"
    return RedirectResponse(dest, status_code=303)


@app.post("/post/{post_id}/like")
async def toggle_like(request: Request, post_id: int, db=Depends(get_db),
                      user=Depends(current_user)):
    redir = login_required(user)
    if redir:
        return redir
    cur = await db.execute("SELECT id FROM posts WHERE id = ?", (post_id,))
    if not await cur.fetchone():
        return RedirectResponse("/feed?msg=Post+not+found", status_code=303)
    cur = await db.execute("SELECT 1 FROM likes WHERE user_id = ? AND post_id = ?",
                           (user["id"], post_id))
    if await cur.fetchone():
        await db.execute("DELETE FROM likes WHERE user_id = ? AND post_id = ?",
                         (user["id"], post_id))
    else:
        try:
            await db.execute("INSERT INTO likes (user_id, post_id, created_at) VALUES (?, ?, ?)",
                             (user["id"], post_id, _now()))
        except sqlite3.IntegrityError:
            pass  # raced double-tap: the like already landed
    await db.commit()
    back = request.headers.get("referer", "/feed")
    return RedirectResponse(back, status_code=303)


# --------------------------------------------------------------------------
# Comment image uploads: phone photos arrive huge (4000px+), so downscale
# server-side before storing. Animated GIFs pass through untouched.
# --------------------------------------------------------------------------
COMMENT_IMG_MAX_DIM = 1600  # longest side, px -- never upscale
COMMENT_IMG_QUALITY = 82    # JPEG/WebP quality at full size
# Fallback steps (max_dim, quality) tried in order when the processed file
# still exceeds MAX_UPLOAD_BYTES; the first step that fits wins.
_COMMENT_IMG_STEPS = ((1600, 82), (1280, 75), (1024, 68), (800, 60))


def _resize_comment_image(data: bytes, ext: str) -> bytes | None:
    """Normalize an uploaded comment image for storage.

    Applies EXIF orientation, strips EXIF, and downscales so the longest
    side is at most COMMENT_IMG_MAX_DIM (aspect preserved, Lanczos, never
    upscaled). Animated GIFs are returned byte-identical. Returns the
    smallest encode that fits under MAX_UPLOAD_BYTES, or the smallest
    attempt (over the cap -- the caller rejects it), or None when the
    bytes cannot be decoded as an image.
    """
    try:
        im = Image.open(io.BytesIO(data))
        animated = ext == ".gif" and getattr(im, "n_frames", 1) > 1
    except Exception:
        return None
    if animated:
        return data
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    w, h = im.size
    has_exif = bool(im.info.get("exif"))
    if max(w, h) <= COMMENT_IMG_MAX_DIM and not has_exif:
        return data  # small and clean: store byte-identical
    fmt = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "gif": "GIF",
           "webp": "WEBP"}[ext.lstrip(".")]
    best = data
    for max_dim, quality in _COMMENT_IMG_STEPS:
        frame = im
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            frame = im.resize((round(w * scale), round(h * scale)),
                               Image.LANCZOS)
        save_kw: dict = {"format": fmt, "optimize": True}
        if fmt == "JPEG":
            if frame.mode in ("RGBA", "LA", "P"):
                frame = frame.convert("RGB")
            save_kw.update(quality=quality, progressive=True)
        elif fmt == "WEBP":
            save_kw.update(quality=quality, method=6)
        # PNG/GIF: lossless, optimize only -- quality steps do not apply.
        buf = io.BytesIO()
        try:
            frame.save(buf, **save_kw)
        except Exception:
            return None
        best = buf.getvalue()
        if len(best) <= MAX_UPLOAD_BYTES:
            return best
    return best


@app.post("/post/{post_id}/comment")
async def add_comment(request: Request, post_id: int, db=Depends(get_db),
                      user=Depends(current_user), body: str = Form(...),
                      gif_url: str = Form(""),
                      image: UploadFile = File(None), video: UploadFile = File(None)):
    redir = login_required(user)
    if redir:
        return redir
    body = body.strip()[:500]
    gif_url = gif_url.strip()[:500]
    if gif_url and not gif_url.startswith(("http://", "https://")):
        gif_url = ""
    if (body or gif_url) and await _recent_duplicate(
            db, "comments",
            "post_id = ? AND user_id = ? AND body = ? AND gif_url = ?",
            (post_id, user["id"], body, gif_url)):
        # Double-tap: the first tap already commented this. Don't make a twin.
        return RedirectResponse(request.headers.get("referer", "/feed"), status_code=303)
    image_path = None
    if image and image.filename:
        ext = os.path.splitext(image.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            return RedirectResponse("/feed?msg=Image+must+be+JPG/PNG/GIF/WebP", status_code=303)
        data = await image.read()
        if len(data) > MAX_VIDEO_BYTES:
            return RedirectResponse("/feed?msg=Image+too+large+(5MB+max)", status_code=303)
        data = _resize_comment_image(data, ext)
        if data is None:
            return RedirectResponse("/feed?msg=Image+not+readable", status_code=303)
        if len(data) > MAX_UPLOAD_BYTES:
            return RedirectResponse("/feed?msg=Image+too+large+(5MB+max)", status_code=303)
        name = f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(COMMENT_IMG_DIR, name), "wb") as f:
            f.write(data)
        image_path = f"comments/{name}"
    video_path = None
    if video and video.filename:
        vext = os.path.splitext(video.filename)[1].lower()
        if vext not in VIDEO_EXTS:
            return RedirectResponse("/feed?msg=Video+must+be+MP4/MOV/WebM", status_code=303)
        vdata = await video.read()
        if len(vdata) > MAX_VIDEO_BYTES:
            return RedirectResponse("/feed?msg=Video+too+large+(50MB+max)", status_code=303)
        vname = f"{uuid.uuid4().hex}{vext}"
        with open(os.path.join(COMMENT_IMG_DIR, vname), "wb") as f:
            f.write(vdata)
        video_path = f"comments/{vname}"
    if body or gif_url or image_path or video_path:
        await db.execute(
            "INSERT INTO comments (post_id, user_id, body, gif_url, image_path, video_path, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (post_id, user["id"], body, gif_url, image_path, video_path, _now()))
        await db.commit()
    back = request.headers.get("referer", "/feed")
    return RedirectResponse(back, status_code=303)


# --------------------------------------------------------------------------
# GIF comments (Tenor). The key comes from TENOR_API_KEY; when unset the route
# answers 503 and the picker button hides itself -- the site works fine
# without a key.
# --------------------------------------------------------------------------
@app.get("/api/gif/search")
async def gif_search(request: Request, q: str = "", user=Depends(current_user)):
    redir = login_required(user)
    if redir:
        return redir
    key = _tenor_key()
    if not key:
        return JSONResponse({"error": "GIF search is not configured"}, status_code=503)
    query = q.strip()[:100]
    if query:
        tenor_url = "https://tenor.googleapis.com/v2/search"
        params = {"q": query, "key": key, "client_key": "budzbook",
                  "limit": 24, "media_filter": "gif"}
    else:
        tenor_url = "https://tenor.googleapis.com/v2/featured"
        params = {"key": key, "client_key": "budzbook", "limit": 24,
                  "media_filter": "gif"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(tenor_url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return JSONResponse({"error": "GIF search failed"}, status_code=502)
    out = []
    for item in data.get("results", [])[:24]:
        fmts = item.get("media_formats") or {}
        url = (fmts.get("gif") or {}).get("url")
        if not url:
            continue
        out.append({"url": url,
                    "preview": (fmts.get("tinygif") or {}).get("url") or url})
    return {"results": out}



# ---- Find a Dispo (Google Places) ----
def _google_places_key() -> str:
    """Google Places/Geocoding API key from env (GOOGLE_PLACES_API_KEY User env var on the PC)."""
    return os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()


_DISPO_CACHE: dict = {}
_DISPO_CACHE_TTL = 15 * 60  # quota care: 15-minute server-side cache


def _dispo_cache_key(lat: float, lng: float) -> str:
    return f"{round(lat, 2)},{round(lng, 2)}"


def _haversine_mi(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in miles."""
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@app.get("/dispo")
async def dispo_page(request: Request, user=Depends(current_user), msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    return templates.TemplateResponse(request, "dispo.html", {
        "user": user, "msg": msg,
        "configured": bool(_google_places_key()),
    })


@app.get("/api/dispo/search")
async def dispo_search(request: Request, lat: str = "", lng: str = "",
                       user=Depends(current_user)):
    redir = login_required(user)
    if redir:
        return redir
    key = _google_places_key()
    if not key:
        return JSONResponse({"error": "Dispensary finder is not configured"},
                            status_code=503)
    try:
        flat, flng = float(lat), float(lng)
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid coordinates"}, status_code=400)
    if not (-90.0 <= flat <= 90.0 and -180.0 <= flng <= 180.0):
        return JSONResponse({"error": "Invalid coordinates"}, status_code=400)
    ckey = _dispo_cache_key(flat, flng)
    now = time.time()
    hit = _DISPO_CACHE.get(ckey)
    if hit and now - hit[0] < _DISPO_CACHE_TTL:
        return {"results": hit[1]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
                params={"location": f"{flat},{flng}", "rankby": "distance",
                        "keyword": "cannabis dispensary", "key": key})
            r.raise_for_status()
            data = r.json()
    except Exception:
        return JSONResponse({"error": "Dispensary search failed"}, status_code=502)
    if data.get("status") not in ("OK", "ZERO_RESULTS"):
        return JSONResponse({"error": "Dispensary search failed"}, status_code=502)
    out = []
    for item in data.get("results", [])[:20]:
        geom = (item.get("geometry") or {}).get("location") or {}
        glat, glng = geom.get("lat"), geom.get("lng")
        dist = (_haversine_mi(flat, flng, glat, glng)
                if isinstance(glat, (int, float)) and isinstance(glng, (int, float))
                else None)
        out.append({
            "name": item.get("name") or "Dispensary",
            "address": item.get("vicinity") or "",
            "rating": item.get("rating"),
            "ratings_total": item.get("user_ratings_total") or 0,
            "open_now": (item.get("opening_hours") or {}).get("open_now"),
            "distance_mi": round(dist, 1) if dist is not None else None,
            "place_id": item.get("place_id") or "",
        })
    out.sort(key=lambda d: (d["distance_mi"] is None, d["distance_mi"] or 0))
    _DISPO_CACHE[ckey] = (now, out)
    return {"results": out}


@app.get("/api/dispo/geocode")
async def dispo_geocode(request: Request, q: str = "", user=Depends(current_user)):
    redir = login_required(user)
    if redir:
        return redir
    key = _google_places_key()
    if not key:
        return JSONResponse({"error": "Dispensary finder is not configured"},
                            status_code=503)
    query = q.strip()[:120]
    if not query:
        return JSONResponse({"error": "Enter a ZIP or city"}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": query, "key": key})
            r.raise_for_status()
            data = r.json()
    except Exception:
        return JSONResponse({"error": "Location lookup failed"}, status_code=502)
    results = data.get("results") or []
    if not results:
        return JSONResponse({"error": "No matching location"}, status_code=404)
    loc = (results[0].get("geometry") or {}).get("location") or {}
    return {"lat": loc.get("lat"), "lng": loc.get("lng"),
            "formatted": results[0].get("formatted_address") or query}


@app.post("/post/{post_id}/delete")
async def delete_post(request: Request, post_id: int, db=Depends(get_db),
                      user=Depends(current_user)):
    redir = login_required(user)
    if redir:
        return redir
    cur = await db.execute("SELECT user_id, image_path FROM posts WHERE id = ?", (post_id,))
    row = await cur.fetchone()
    if row and (row["user_id"] == user["id"] or user["is_admin"]):
        if row["image_path"]:
            try:
                os.remove(os.path.join(MEDIA_DIR, row["image_path"]))
            except OSError:
                pass
        await db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        await db.commit()
        msg = "Post+deleted"
    else:
        msg = "Can't+delete+that+post"
    back = request.headers.get("referer", "/feed")
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}msg={msg}", status_code=303)


@app.post("/post/{post_id}/report")
async def report_post(request: Request, post_id: int, db=Depends(get_db),
                      user=Depends(current_user), reason: str = Form("")):
    redir = login_required(user)
    if redir:
        return redir
    cur = await db.execute("SELECT user_id FROM posts WHERE id = ?", (post_id,))
    row = await cur.fetchone()
    if not row:
        return RedirectResponse("/feed?msg=Post+not+found", status_code=303)
    if row["user_id"] == user["id"]:
        return RedirectResponse("/feed?msg=You+can't+report+your+own+post", status_code=303)
    reason = reason.strip()[:300]
    if await _recent_duplicate(
            db, "reports",
            "post_id = ? AND reporter_id = ? AND reason = ?",
            (post_id, user["id"], reason)):
        # Double-tap: already reported. Don't stack a twin report.
        return RedirectResponse("/feed?msg=Reported+-+mods+will+take+a+look", status_code=303)
    await db.execute(
        "INSERT INTO reports (post_id, reporter_id, reason, created_at) VALUES (?, ?, ?, ?)",
        (post_id, user["id"], reason, _now()))
    await db.commit()
    return RedirectResponse("/feed?msg=Reported+-+mods+will+take+a+look", status_code=303)


# --------------------------------------------------------------------------
# Profiles & follows
# --------------------------------------------------------------------------
async def _profile_ctx(db, username, me):
    cur = await db.execute("SELECT * FROM users WHERE username = ?", (username,))
    profile = await cur.fetchone()
    if not profile:
        return None
    profile = dict(profile)
    cur = await db.execute("SELECT COUNT(*) n FROM follows WHERE followed_id = ?", (profile["id"],))
    profile["follower_count"] = (await cur.fetchone())["n"]
    cur = await db.execute("SELECT COUNT(*) n FROM follows WHERE follower_id = ?", (profile["id"],))
    profile["following_count"] = (await cur.fetchone())["n"]
    profile["is_following"] = False
    if me and me["id"] != profile["id"]:
        cur = await db.execute(
            "SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?",
            (me["id"], profile["id"]))
        profile["is_following"] = bool(await cur.fetchone())
    profile["has_avatar"] = avatar_exists(profile["username"])
    profile["figurine"] = figurine_exists(profile["username"])
    posts = await _post_rows(db, me["id"] if me else 0,
                             "WHERE p.user_id = ?", (profile["id"],), 50, 0)
    return profile, posts


@app.get("/u/{username}")
async def profile(request: Request, username: str, db=Depends(get_db),
                  user=Depends(current_user), msg: str = ""):
    ctx = await _profile_ctx(db, username, user)
    if not ctx:
        return templates.TemplateResponse(request, "404.html", { "user": user},
                                          status_code=404)
    profile, posts = ctx
    gear = await currency.equipped_gear(db, profile["id"])
    return templates.TemplateResponse(request, "profile.html", { "user": user, "profile": profile, "posts": posts, "msg": msg, "gear": gear})


@app.get("/u/{username}/followers")
async def followers(request: Request, username: str, db=Depends(get_db),
                    user=Depends(current_user)):
    redir = login_required(user)
    if redir:
        return redir
    cur = await db.execute("SELECT * FROM users WHERE username = ?", (username,))
    profile = await cur.fetchone()
    if not profile:
        return RedirectResponse("/feed", status_code=303)
    cur = await db.execute(
        """SELECT u.* FROM follows f JOIN users u ON u.id = f.follower_id
           WHERE f.followed_id = ? ORDER BY u.username""", (profile["id"],))
    people = [dict(r) for r in await cur.fetchall()]
    return templates.TemplateResponse(request, "people.html", { "user": user, "profile": dict(profile),
        "people": people, "title": "Followers"})


@app.get("/u/{username}/following")
async def following(request: Request, username: str, db=Depends(get_db),
                    user=Depends(current_user)):
    redir = login_required(user)
    if redir:
        return redir
    cur = await db.execute("SELECT * FROM users WHERE username = ?", (username,))
    profile = await cur.fetchone()
    if not profile:
        return RedirectResponse("/feed", status_code=303)
    cur = await db.execute(
        """SELECT u.* FROM follows f JOIN users u ON u.id = f.followed_id
           WHERE f.follower_id = ? ORDER BY u.username""", (profile["id"],))
    people = [dict(r) for r in await cur.fetchall()]
    return templates.TemplateResponse(request, "people.html", { "user": user, "profile": dict(profile),
        "people": people, "title": "Following"})


@app.post("/u/{username}/follow")
async def follow(request: Request, username: str, db=Depends(get_db),
                 user=Depends(current_user)):
    redir = login_required(user)
    if redir:
        return redir
    cur = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
    target = await cur.fetchone()
    if target and target["id"] != user["id"]:
        await db.execute(
            "INSERT OR IGNORE INTO follows (follower_id, followed_id, created_at)"
            " VALUES (?, ?, ?)", (user["id"], target["id"], _now()))
        await db.commit()
    return RedirectResponse(f"/u/{username}", status_code=303)


@app.post("/u/{username}/unfollow")
async def unfollow(request: Request, username: str, db=Depends(get_db),
                   user=Depends(current_user)):
    redir = login_required(user)
    if redir:
        return redir
    cur = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
    target = await cur.fetchone()
    if target:
        await db.execute("DELETE FROM follows WHERE follower_id = ? AND followed_id = ?",
                         (user["id"], target["id"]))
        await db.commit()
    return RedirectResponse(f"/u/{username}", status_code=303)


# --------------------------------------------------------------------------
# Settings (profile edit + avatar upload)
# --------------------------------------------------------------------------
@app.get("/settings")
async def settings_form(request: Request, db=Depends(get_db), user=Depends(current_user),
                        msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    try:
        stored_prefs = json.loads(user["feed_prefs"] or "{}")
    except Exception:
        stored_prefs = {}
    feed_ctx = {"sort": "top" if stored_prefs.get("sort") == "top" else "latest",
                "show_reshares": stored_prefs.get("show_reshares", True),
                "muted_words_raw": str(stored_prefs.get("muted_words", ""))}
    return templates.TemplateResponse(request, "settings.html", { "user": user, "msg": msg,
        "has_avatar": avatar_exists(user["username"]), "swatches": THEME_SWATCHES,
        "feed_prefs": feed_ctx})


@app.post("/settings")
async def settings_save(request: Request, db=Depends(get_db), user=Depends(current_user),
                        display_name: str = Form(""), bio: str = Form(""),
                        twitch_username: str = Form(""),
                        avatar: UploadFile = File(None),
                        theme_color: str = Form(""), banner: UploadFile = File(None),
                        feed_sort: str = Form("latest"),
                        feed_show_reshares: str = Form(""),
                        feed_muted_words: str = Form("")):
    redir = login_required(user)
    if redir:
        return redir
    avatar_msg = ""
    if avatar and avatar.filename:
        try:
            process_avatar_upload(avatar, user["username"])
            # Any new/changed upload needs mod approval again before stream use.
            await db.execute("UPDATE users SET avatar_approved = 0 WHERE id = ?",
                             (user["id"],))
            avatar_msg = "+Avatar+uploaded+-+pending+mod+approval"
        except ValueError as e:
            return RedirectResponse(f"/settings?msg={e}", status_code=303)
    # Profile look: theme must be one of the preset swatches; banner is
    # center-cropped to 1200x300.
    theme_color = theme_color.strip().lower()
    if theme_color not in THEME_SWATCHES:
        theme_color = ""
    banner_path = user["banner_path"] or ""
    if banner and banner.filename:
        try:
            banner_path = process_banner_upload(banner, user["username"])
        except ValueError as e:
            return RedirectResponse(f"/settings?msg={e}", status_code=303)
    # Feed prefs, stored as JSON on the user row.
    feed_prefs = json.dumps({
        "sort": "top" if feed_sort.strip() == "top" else "latest",
        "show_reshares": bool(feed_show_reshares),
        "muted_words": feed_muted_words.strip()[:500],
    })
    # A Twitch-verified link is authoritative: manual edits can't change it.
    # Unlinking happens by re-linking a different account via /auth/twitch.
    twitch_handle = (user["twitch_username"] if user["twitch_verified"]
                     else twitch_username.strip()[:40])
    await db.execute(
        "UPDATE users SET display_name = ?, bio = ?, twitch_username = ?, "
        "theme_color = ?, banner_path = ?, feed_prefs = ? WHERE id = ?",
        (display_name.strip()[:60] or user["username"], bio.strip()[:300],
         twitch_handle, theme_color, banner_path, feed_prefs, user["id"]))
    await db.commit()
    return RedirectResponse(f"/settings?msg=Profile+saved{avatar_msg}", status_code=303)


@app.post("/settings/password")
async def settings_password(request: Request, db=Depends(get_db), user=Depends(current_user),
                            current_password: str = Form(""),
                            new_password: str = Form(""),
                            confirm_password: str = Form("")):
    """Let a logged-in user change their own password."""
    redir = login_required(user)
    if redir:
        return redir
    row = await db.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],))
    stored = (await row.fetchone())["password_hash"]
    if not stored or not verify_password(current_password, stored):
        return RedirectResponse("/settings?msg=Current+password+is+wrong", status_code=303)
    if len(new_password) < 6:
        return RedirectResponse("/settings?msg=New+password+needs+at+least+6+characters",
                                status_code=303)
    if new_password != confirm_password:
        return RedirectResponse("/settings?msg=New+passwords+do+not+match", status_code=303)
    await db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (hash_password(new_password), user["id"]))
    await db.commit()
    return RedirectResponse("/settings?msg=Password+changed", status_code=303)


# --------------------------------------------------------------------------
# DMs (1:1, polling — no websockets in v1)
# --------------------------------------------------------------------------
async def _conversations(db, me_id):
    cur = await db.execute(
        """SELECT u.*, MAX(m.id) AS last_id,
                  (SELECT body FROM messages
                    WHERE (sender_id = ? AND recipient_id = u.id)
                       OR (sender_id = u.id AND recipient_id = ?)
                    ORDER BY id DESC LIMIT 1) AS last_body,
                  (SELECT created_at FROM messages
                    WHERE (sender_id = ? AND recipient_id = u.id)
                       OR (sender_id = u.id AND recipient_id = ?)
                    ORDER BY id DESC LIMIT 1) AS last_at
           FROM users u
           JOIN messages m ON (m.sender_id = ? AND m.recipient_id = u.id)
                           OR (m.sender_id = u.id AND m.recipient_id = ?)
           WHERE u.id != ?
           GROUP BY u.id ORDER BY last_id DESC""",
        (me_id, me_id, me_id, me_id, me_id, me_id, me_id))
    return [dict(r) for r in await cur.fetchall()]


@app.get("/messages")
async def dm_list(request: Request, db=Depends(get_db), user=Depends(current_user),
                  msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    convos = await _conversations(db, user["id"])
    # Everyone you follow / who follows you is messageable — list them too.
    cur = await db.execute(
        """SELECT DISTINCT u.* FROM users u
           WHERE u.id != ? AND u.id NOT IN
             (SELECT CASE WHEN sender_id = ? THEN recipient_id ELSE sender_id END
              FROM messages WHERE sender_id = ? OR recipient_id = ?)
           ORDER BY u.username LIMIT 50""",
        (user["id"], user["id"], user["id"], user["id"]))
    others = [dict(r) for r in await cur.fetchall()]
    return templates.TemplateResponse(request, "messages.html", { "user": user, "convos": convos, "others": others, "msg": msg})


@app.get("/messages/{username}")
async def dm_thread(request: Request, username: str, db=Depends(get_db),
                    user=Depends(current_user), msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    cur = await db.execute("SELECT * FROM users WHERE username = ?", (username,))
    peer = await cur.fetchone()
    if not peer or peer["id"] == user["id"]:
        return RedirectResponse("/messages?msg=User+not+found", status_code=303)
    cur = await db.execute(
        """SELECT m.*, s.username AS sender_name FROM messages m
           JOIN users s ON s.id = m.sender_id
           WHERE (m.sender_id = ? AND m.recipient_id = ?)
              OR (m.sender_id = ? AND m.recipient_id = ?)
           ORDER BY m.id DESC LIMIT 50""",
        (user["id"], peer["id"], peer["id"], user["id"]))
    messages = [dict(r) for r in reversed(await cur.fetchall())]
    last_id = messages[-1]["id"] if messages else 0
    return templates.TemplateResponse(request, "conversation.html", { "user": user, "peer": dict(peer),
        "messages": messages, "last_id": last_id, "msg": msg})


@app.post("/messages/{username}/send")
async def dm_send(username: str, request: Request, db=Depends(get_db),
                  user=Depends(current_user), body: str = Form(...)):
    redir = login_required(user)
    if redir:
        return redir
    cur = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
    peer = await cur.fetchone()
    body = body.strip()[:1000]
    if peer and peer["id"] != user["id"] and body:
        if await _recent_duplicate(
                db, "messages",
                "sender_id = ? AND recipient_id = ? AND body = ?",
                (user["id"], peer["id"], body)):
            # Double-tap: the first tap already sent this. Don't send a twin.
            return RedirectResponse(f"/messages/{username}", status_code=303)
        await db.execute(
            "INSERT INTO messages (sender_id, recipient_id, body, created_at)"
            " VALUES (?, ?, ?, ?)", (user["id"], peer["id"], body, _now()))
        await db.commit()
    return RedirectResponse(f"/messages/{username}", status_code=303)


@app.get("/messages/{username}/poll")
async def dm_poll(username: str, request: Request, db=Depends(get_db),
                  user=Depends(current_user), after_id: int = 0):
    """JSON poll endpoint for live-ish DM updates. Returns messages newer
    than after_id in this conversation."""
    if not user:
        return JSONResponse({"error": "login"}, status_code=401)
    cur = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
    peer = await cur.fetchone()
    if not peer:
        return JSONResponse({"messages": []})
    cur = await db.execute(
        """SELECT m.id, m.body, m.created_at, s.username AS sender_name,
                  (m.sender_id = ?) AS mine
           FROM messages m JOIN users s ON s.id = m.sender_id
           WHERE m.id > ? AND ((m.sender_id = ? AND m.recipient_id = ?)
                            OR (m.sender_id = ? AND m.recipient_id = ?))
           ORDER BY m.id ASC""",
        (user["id"], after_id, user["id"], peer["id"], peer["id"], user["id"]))
    return JSONResponse({"messages": [dict(r) for r in await cur.fetchall()]})


# --------------------------------------------------------------------------
# Game currencies: wallets, exchange, faucet, shop (closed-loop, no cash value)
# --------------------------------------------------------------------------
@app.get("/wallet")
async def wallet_page(request: Request, db=Depends(get_db), user=Depends(current_user),
                      msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    wallets = await currency.wallets_for(db, user["id"])
    base = await currency.get_base_currency(db)
    can_claim = True
    if base:
        cur = await db.execute(
            "SELECT claimed_at FROM claims WHERE user_id = ? AND currency_id = ?",
            (user["id"], base["id"]))
        row = await cur.fetchone()
        if row:
            from datetime import datetime, timezone as _tz
            elapsed = (datetime.now(_tz.utc) - datetime.fromisoformat(row["claimed_at"])).total_seconds()
            can_claim = elapsed >= 24 * 3600
    return templates.TemplateResponse(request, "wallet.html", {
        "user": user, "msg": msg, "wallets": wallets,
        "inventory": await currency.inventory_for(db, user["id"]),
        "txns": await currency.recent_txns(db, user["id"]),
        "can_claim": can_claim, "claim_amount": currency.DAILY_CLAIM_AMOUNT,
    })


@app.post("/wallet/claim")
async def wallet_claim(request: Request, db=Depends(get_db), user=Depends(current_user)):
    redir = login_required(user)
    if redir:
        return redir
    ok, msg = await currency.claim_daily(db, user["id"])
    return RedirectResponse(f"/wallet?msg={msg}", status_code=303)


@app.get("/exchange")
async def exchange_page(request: Request, db=Depends(get_db), user=Depends(current_user),
                        msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    return templates.TemplateResponse(request, "exchange.html", {
        "user": user, "msg": msg,
        "currencies": await currency.get_currencies(db),
        "wallets": await currency.wallets_for(db, user["id"]),
        "fee_pct": int(currency.EXCHANGE_FEE * 100),
    })


@app.post("/exchange")
async def exchange_do(request: Request, db=Depends(get_db), user=Depends(current_user),
                      from_code: str = Form(...), to_code: str = Form(...),
                      amount: str = Form(...)):
    redir = login_required(user)
    if redir:
        return redir
    try:
        amount_in = int(amount)
    except ValueError:
        return RedirectResponse("/exchange?msg=Amount+must+be+a+whole+number", status_code=303)
    f_code, t_code = from_code.strip().upper(), to_code.strip().upper()
    cur = await db.execute(
        "SELECT 1 FROM currency_txns WHERE user_id = ? AND reason = ?"
        " AND delta = ? AND created_at >= ? LIMIT 1",
        (user["id"], f"swap {f_code}->{t_code} (2% fee)",
         -amount_in, _dedup_cutoff()))
    if await cur.fetchone():
        # Double-tap: the first tap already swapped this. Don't swap twice.
        return RedirectResponse("/exchange?msg=Swap+already+processed",
                                status_code=303)
    ok, msg, _out = await currency.swap(db, user["id"], f_code, t_code, amount_in)
    return RedirectResponse(f"/exchange?msg={msg}", status_code=303)


@app.get("/shop")
async def shop_page(request: Request, db=Depends(get_db), user=Depends(current_user),
                    msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    items = await currency.get_shop_items(db)
    owned = {i["id"] for i in await currency.inventory_for(db, user["id"])}
    return templates.TemplateResponse(request, "shop.html", {
        "user": user, "msg": msg, "items": items, "owned": owned,
        "wallets": await currency.wallets_for(db, user["id"]),
    })


@app.post("/shop/buy/{item_id}")
async def shop_buy(item_id: int, request: Request, db=Depends(get_db),
                   user=Depends(current_user)):
    redir = login_required(user)
    if redir:
        return redir
    ok, msg = await currency.buy_item(db, user["id"], item_id)
    return RedirectResponse(f"/shop?msg={msg}", status_code=303)


@app.post("/inventory/equip/{item_id}")
async def inventory_equip(item_id: int, request: Request, db=Depends(get_db),
                          user=Depends(current_user)):
    redir = login_required(user)
    if redir:
        return redir
    ok, msg = await currency.equip_item(db, user["id"], item_id)
    return RedirectResponse(f"/wallet?msg={msg}", status_code=303)


@app.post("/admin/currency/create")
async def admin_currency_create(request: Request, db=Depends(get_db),
                                user=Depends(current_user),
                                code: str = Form(...), name: str = Form(...),
                                owner: str = Form(""), icon: str = Form("🪙"),
                                rate_to_base: str = Form("1.0")):
    """Register a streamer's currency (fixed rate vs Budz)."""
    redir = admin_required(user)
    if redir:
        return redir
    code = code.strip().upper()
    try:
        rate = float(rate_to_base)
        assert rate > 0
    except (ValueError, AssertionError):
        return RedirectResponse("/admin?msg=Rate+must+be+a+positive+number", status_code=303)
    if not re.fullmatch(r"[A-Z]{2,8}", code):
        return RedirectResponse("/admin?msg=Code+must+be+2-8+letters", status_code=303)
    try:
        await db.execute(
            "INSERT INTO currencies (code, name, owner, icon, rate_to_base, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (code, name.strip()[:40] or code, owner.strip()[:40],
             icon.strip()[:8] or "🪙", rate, _now()))
        await db.commit()
        msg = f"Currency+{code}+created"
    except Exception:
        msg = f"{code}+already+exists"
    return RedirectResponse(f"/admin?msg={msg}", status_code=303)


@app.post("/admin/currency/grant")
async def admin_currency_grant(request: Request, db=Depends(get_db),
                               user=Depends(current_user),
                               username: str = Form(...), code: str = Form(...),
                               amount: str = Form(...)):
    """Mod grant: credit any user any currency (event prizes, corrections)."""
    redir = admin_required(user)
    if redir:
        return redir
    cur = await db.execute("SELECT id FROM users WHERE username = ?",
                           (username.strip(),))
    target = await cur.fetchone()
    try:
        amount = int(amount)
        assert amount > 0
    except (ValueError, AssertionError):
        return RedirectResponse("/admin?msg=Amount+must+be+a+positive+number",
                                status_code=303)
    if not target:
        return RedirectResponse("/admin?msg=User+not+found", status_code=303)
    code = code.strip().upper()
    cur = await db.execute(
        """SELECT 1 FROM currency_txns t JOIN currencies c ON c.id = t.currency_id
           WHERE t.user_id = ? AND c.code = ? AND t.delta = ? AND t.reason = ?
             AND t.created_at >= ? LIMIT 1""",
        (target["id"], code, amount, f"mod grant by {user['username']}",
         _dedup_cutoff()))
    if await cur.fetchone():
        # Double-tap: the first tap already granted this. Don't grant twice.
        msg = "Grant+already+processed"
    else:
        try:
            await currency.award(db, target["id"], code, amount,
                                 f"mod grant by {user['username']}")
            msg = f"Granted+{amount}+{code}+to+{username.strip()}"
        except ValueError as e:
            msg = str(e)
    return RedirectResponse(f"/admin?msg={msg}", status_code=303)


# --------------------------------------------------------------------------
# THC tie-in pages
# --------------------------------------------------------------------------
@app.get("/leaderboard")
async def leaderboard(request: Request, user=Depends(current_user), msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    board, live = thc_adapter.get_casino_leaderboard()
    return templates.TemplateResponse(request, "leaderboard.html", { "user": user, "msg": msg,
        "board": board, "mock": not live})


@app.get("/sportsbook")
async def sportsbook_page(request: Request, user=Depends(current_user), msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    return templates.TemplateResponse(request, "sportsbook.html", { "user": user, "msg": msg,
        "lines": thc_adapter.get_sportsbook_lines(),
        "mock": thc_adapter.MOCK})


@app.get("/grow")
async def grow_page(request: Request, db=Depends(get_db), user=Depends(current_user),
                    msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    posts = await _post_rows(db, user["id"], "WHERE p.kind = 'grow'", (), 30, 0)
    return templates.TemplateResponse(request, "grow.html", { "user": user, "msg": msg, "posts": posts,
        "stats": thc_adapter.get_grow_stats(), "mock": thc_adapter.MOCK})


@app.get("/crew")
async def crew_page(request: Request, db=Depends(get_db), user=Depends(current_user),
                    msg: str = ""):
    """Public showcase of everyone with a 3D avatar (the 3D Crew)."""
    cur = await db.execute(
        "SELECT username, display_name, avatar_3d_url FROM users"
        " WHERE avatar_3d_url != '' ORDER BY id")
    crew = [dict(r) for r in await cur.fetchall()]
    return templates.TemplateResponse(request, "crew.html", { "user": user, "msg": msg,
        "crew": crew})


# --------------------------------------------------------------------------
# Admin: reports + avatar approvals
# --------------------------------------------------------------------------
@app.get("/admin")
async def admin_page(request: Request, db=Depends(get_db), user=Depends(current_user),
                     msg: str = ""):
    redir = admin_required(user)
    if redir:
        return redir
    cur = await db.execute(
        """SELECT r.*, p.body AS post_body, p.image_path,
                  u.username AS post_author, ru.username AS reporter
           FROM reports r
           JOIN posts p ON p.id = r.post_id
           JOIN users u ON u.id = p.user_id
           JOIN users ru ON ru.id = r.reporter_id
           WHERE r.resolved = 0 ORDER BY r.created_at DESC""")
    reports = [dict(r) for r in await cur.fetchall()]
    cur = await db.execute(
        "SELECT username, display_name, twitch_username FROM users WHERE avatar_approved = 0")
    pending = [dict(r) for r in await cur.fetchall() if avatar_exists(r["username"])]
    return templates.TemplateResponse(request, "admin.html", { "user": user, "msg": msg,
        "reports": reports, "pending_avatars": pending,
        "currencies": await currency.get_currencies(db)})


@app.post("/admin/avatar/{username}/approve")
async def avatar_approve(username: str, db=Depends(get_db), user=Depends(current_user)):
    redir = admin_required(user)
    if redir:
        return redir
    await db.execute("UPDATE users SET avatar_approved = 1 WHERE username = ?", (username,))
    await db.commit()
    return RedirectResponse(f"/admin?msg=Avatar+approved+for+{username}", status_code=303)


@app.post("/admin/avatar/{username}/reject")
async def avatar_reject(username: str, db=Depends(get_db), user=Depends(current_user)):
    redir = admin_required(user)
    if redir:
        return redir
    try:
        os.remove(avatar_path_for(username))
    except OSError:
        pass
    await db.execute("UPDATE users SET avatar_approved = 0 WHERE username = ?", (username,))
    await db.commit()
    return RedirectResponse(f"/admin?msg=Avatar+rejected+for+{username}", status_code=303)


@app.post("/admin/reports/{report_id}/resolve")
async def report_resolve(request: Request, report_id: int, db=Depends(get_db),
                         user=Depends(current_user), action: str = Form("dismiss")):
    """Resolve a report. action=delete removes the post too; dismiss just closes it."""
    redir = admin_required(user)
    if redir:
        return redir
    cur = await db.execute("SELECT post_id FROM reports WHERE id = ?", (report_id,))
    row = await cur.fetchone()
    if row:
        if action == "delete":
            cur2 = await db.execute("SELECT image_path FROM posts WHERE id = ?",
                                    (row["post_id"],))
            prow = await cur2.fetchone()
            if prow and prow["image_path"]:
                try:
                    os.remove(os.path.join(MEDIA_DIR, prow["image_path"]))
                except OSError:
                    pass
            await db.execute("DELETE FROM posts WHERE id = ?", (row["post_id"],))
        await db.execute("UPDATE reports SET resolved = 1 WHERE id = ?", (report_id,))
        await db.commit()
    return RedirectResponse("/admin?msg=Report+resolved", status_code=303)


# ---------- Stream overlays: score ticker + live look (OBS browser sources) ----------
@app.get("/ticker")
async def ticker_page(request: Request):
    """ESPN-style bottom score ticker. OBS Browser Source: 1920x64."""
    return templates.TemplateResponse(request, "ticker.html", {"user": None})


@app.get("/ticker.json")
async def ticker_data():
    return JSONResponse(ticker.get_scores())


@app.get("/livelook")
async def livelook_page(request: Request):
    """Live-look stat window, rotates game to game. OBS Browser Source: 560x420."""
    return templates.TemplateResponse(request, "livelook.html", {"user": None})


@app.get("/livelook.json")
async def livelook_data():
    return JSONResponse(ticker.get_live_look())
PICKS_PATH = os.path.join(BASE_DIR, "budz_picks.json")


def load_budz_picks():
    """Today's Budz Picks sheet (written daily by budz_picks.py)."""
    try:
        with open(PICKS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and (data.get("picks") or data.get("tickets")):
            return data
    except Exception:
        pass
    return {"date": "", "picks": [], "parlay": None,
            "note": "Today's sheet is still brewing \u2014 check back soon."}






# ---------------- trending + hashtags (X-style) ----------------
@app.get("/trending")
async def trending(request: Request, db=Depends(get_db), user=Depends(current_user),
                   msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    cur = await db.execute(
        """SELECT h.tag, COUNT(*) AS n FROM post_hashtags h
           JOIN posts p ON p.id = h.post_id
           WHERE p.created_at > ?
           GROUP BY h.tag ORDER BY n DESC, h.tag ASC LIMIT 25""",
        (week_ago,))
    tags = [dict(r) for r in await cur.fetchall()]
    return templates.TemplateResponse(request, "trending.html",
        {"user": user, "tags": tags, "msg": msg})


@app.get("/tag/{tag}")
async def tag_page(request: Request, tag: str, db=Depends(get_db),
                   user=Depends(current_user), page: int = 1, msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    tag = tag.lower()
    page = max(1, page)
    offset = (page - 1) * FEED_PAGE_SIZE
    where = "WHERE p.id IN (SELECT post_id FROM post_hashtags WHERE tag = ?)"
    posts = await _post_rows(db, user["id"], where, (tag,), FEED_PAGE_SIZE + 1, offset)
    has_more = len(posts) > FEED_PAGE_SIZE
    return templates.TemplateResponse(request, "tag.html",
        {"user": user, "tag": tag, "posts": posts[:FEED_PAGE_SIZE],
         "page": page, "has_more": has_more, "msg": msg})


# ---------------- groups (Facebook-style) ----------------
@app.get("/groups")
async def groups_list(request: Request, db=Depends(get_db), user=Depends(current_user),
                      msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    cur = await db.execute(
        """SELECT g.*, u.username AS owner_name,
                  (SELECT COUNT(*) FROM group_members m WHERE m.group_id = g.id) AS member_count,
                  (SELECT COUNT(*) FROM group_members m2
                   WHERE m2.group_id = g.id AND m2.user_id = ?) AS is_member
           FROM groups g JOIN users u ON u.id = g.owner_id
           ORDER BY member_count DESC, g.created_at DESC""",
        (user["id"],))
    groups = [dict(r) for r in await cur.fetchall()]
    return templates.TemplateResponse(request, "groups.html",
        {"user": user, "groups": groups, "msg": msg})


@app.post("/groups")
async def groups_create(request: Request, db=Depends(get_db), user=Depends(current_user),
                        name: str = Form(...), description: str = Form("")):
    redir = login_required(user)
    if redir:
        return redir
    name = name.strip()[:60]
    if not name:
        return RedirectResponse("/groups?msg=Group+needs+a+name", status_code=303)
    try:
        cur = await db.execute(
            "INSERT INTO groups (name, description, owner_id, created_at)"
            " VALUES (?, ?, ?, ?)",
            (name, description.strip()[:500], user["id"], _now()))
        gid = cur.lastrowid
        await db.execute(
            "INSERT INTO group_members (group_id, user_id, joined_at) VALUES (?, ?, ?)",
            (gid, user["id"], _now()))
        await db.commit()
    except Exception:
        await db.rollback()
        return RedirectResponse("/groups?msg=That+name+is+taken", status_code=303)
    return RedirectResponse(f"/groups/{gid}?msg=Group+created", status_code=303)


@app.get("/groups/{gid}")
async def group_page(request: Request, gid: int, db=Depends(get_db),
                     user=Depends(current_user), page: int = 1, msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    cur = await db.execute(
        """SELECT g.*, u.username AS owner_name,
                  (SELECT COUNT(*) FROM group_members m WHERE m.group_id = g.id) AS member_count
           FROM groups g JOIN users u ON u.id = g.owner_id WHERE g.id = ?""",
        (gid,))
    row = await cur.fetchone()
    if not row:
        return RedirectResponse("/groups?msg=Group+not+found", status_code=303)
    group = dict(row)
    cur = await db.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
        (gid, user["id"]))
    group["is_member"] = bool(await cur.fetchone())
    page = max(1, page)
    offset = (page - 1) * FEED_PAGE_SIZE
    posts = await _post_rows(db, user["id"], "WHERE p.group_id = ?", (gid,),
                             FEED_PAGE_SIZE + 1, offset)
    has_more = len(posts) > FEED_PAGE_SIZE
    return templates.TemplateResponse(request, "group.html",
        {"user": user, "group": group, "posts": posts[:FEED_PAGE_SIZE],
         "page": page, "has_more": has_more, "msg": msg})


@app.post("/groups/{gid}/join")
async def group_join(request: Request, gid: int, db=Depends(get_db),
                     user=Depends(current_user)):
    redir = login_required(user)
    if redir:
        return redir
    await db.execute(
        "INSERT OR IGNORE INTO group_members (group_id, user_id, joined_at)"
        " VALUES (?, ?, ?)", (gid, user["id"], _now()))
    await db.commit()
    return RedirectResponse(f"/groups/{gid}?msg=Joined", status_code=303)


@app.post("/groups/{gid}/leave")
async def group_leave(request: Request, gid: int, db=Depends(get_db),
                      user=Depends(current_user)):
    redir = login_required(user)
    if redir:
        return redir
    await db.execute(
        "DELETE FROM group_members WHERE group_id = ? AND user_id = ?",
        (gid, user["id"]))
    await db.commit()
    return RedirectResponse(f"/groups/{gid}?msg=Left", status_code=303)


# ---------------- clips: vertical short-video feed (TikTok-style) ----------------
@app.get("/clips")
async def clips(request: Request, db=Depends(get_db), user=Depends(current_user),
                page: int = 1, msg: str = ""):
    redir = login_required(user)
    if redir:
        return redir
    page = max(1, page)
    offset = (page - 1) * FEED_PAGE_SIZE
    posts = await _post_rows(db, user["id"], "WHERE p.video_path IS NOT NULL",
                             (), FEED_PAGE_SIZE + 1, offset)
    has_more = len(posts) > FEED_PAGE_SIZE
    return templates.TemplateResponse(request, "clips.html",
        {"user": user, "posts": posts[:FEED_PAGE_SIZE],
         "page": page, "has_more": has_more, "msg": msg})


# ---------------- followed-streams watch grid (Twitch) ----------------
_twitch_app_token = {"token": None, "exp": 0.0}


async def _twitch_app_token(hc):
    """Client-credentials app token for Helix, cached ~50 minutes. None when unconfigured."""
    now = time.time()
    if _twitch_app_token["token"] and now < _twitch_app_token["exp"]:
        return _twitch_app_token["token"]
    if not twitch_oauth.configured():
        return None
    tr = await hc.post("https://id.twitch.tv/oauth2/token",
                       params={"client_id": twitch_oauth._client_id(),
                               "client_secret": twitch_oauth._client_secret(),
                               "grant_type": "client_credentials"})
    tr.raise_for_status()
    tok = tr.json()["access_token"]
    _twitch_app_token.update(token=tok, exp=now + 3000)
    return tok


_watch_status_cache = {"ts": 0.0, "data": []}


async def get_watch_channels_status(channels):
    """Live/offline status for each login in *channels* (order preserved).

    Returns a list of dicts: login, display_name, is_live, title, game_name,
    viewer_count, thumbnail_url, profile_image_url, offline_image_url,
    started_at. Fail-soft: [] when Twitch is unreachable or unconfigured.
    Results are cached 90 seconds so page loads stay cheap.
    """
    chans = [c.strip().lower() for c in channels if c and c.strip()]
    if not chans:
        return []
    now = time.time()
    if now - _watch_status_cache["ts"] < 90 and _watch_status_cache["data"]:
        cached = [c for c in _watch_status_cache["data"] if c["login"] in chans]
        if len(cached) == len(chans):
            return cached
    out = []
    try:
        async with httpx.AsyncClient(timeout=12) as hc:
            token = await _twitch_app_token(hc)
            if not token:
                return []
            headers = {"Client-Id": twitch_oauth._client_id(),
                       "Authorization": f"Bearer {token}"}
            users = {}
            ur = await hc.get("https://api.twitch.tv/helix/users",
                              params=[("login", c) for c in chans],
                              headers=headers)
            ur.raise_for_status()
            for u in ur.json().get("data", []):
                users[u.get("login", "").lower()] = u
            live = {}
            sr = await hc.get("https://api.twitch.tv/helix/streams",
                              params=[("user_login", c) for c in chans],
                              headers=headers)
            sr.raise_for_status()
            for s in sr.json().get("data", []):
                live[s.get("user_login", "").lower()] = s
            for login in chans:
                u = users.get(login, {})
                s = live.get(login)
                thumb = ((s or {}).get("thumbnail_url") or "")
                thumb = thumb.replace("{width}", "640").replace("{height}", "360")
                out.append({
                    "login": login,
                    "display_name": u.get("display_name") or login,
                    "is_live": bool(s),
                    "title": (s or {}).get("title") or "",
                    "game_name": (s or {}).get("game_name") or "",
                    "viewer_count": (s or {}).get("viewer_count") or 0,
                    "thumbnail_url": thumb,
                    "profile_image_url": u.get("profile_image_url") or "",
                    "offline_image_url": u.get("offline_image_url") or "",
                    "started_at": (s or {}).get("started_at") or "",
                })
        _watch_status_cache.update(ts=now, data=out)
    except Exception:
        return []
    return out


def _watch_embeds(request, channel):
    """Twitch player + chat embed URLs for a channel (parent domains handled)."""
    host = (request.headers.get("host") or "").split(":")[0].strip()
    parents = list(dict.fromkeys(p for p in [*TWITCH_EMBED_PARENTS, host] if p))
    parent_qs = "&".join(f"parent={p}" for p in parents)
    player_url = (f"https://player.twitch.tv/?channel={channel}"
                  f"&muted=true&{parent_qs}")
    chat_url = (f"https://www.twitch.tv/embed/{channel}/chat"
                f"?darkpopout&{parent_qs}")
    return player_url, chat_url


# ---------------- go-live alerts (Twitch) ----------------
async def maybe_post_golive(db):
    """If Brad just went live and we haven't posted for this stream, post to the feed."""
    try:
        cur = await db.execute("SELECT value FROM meta WHERE key = 'golive_last_check'")
        row = await cur.fetchone()
        now = datetime.now(timezone.utc)
        if row:
            try:
                last = datetime.fromisoformat(row["value"])
                if (now - last).total_seconds() < 300:
                    return
            except ValueError:
                pass
        await db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('golive_last_check', ?)",
            (now.isoformat(),))
        await db.commit()
        if not twitch_oauth.configured():
            return
        cid = twitch_oauth._client_id()
        csec = twitch_oauth._client_secret()
        async with httpx.AsyncClient(timeout=12) as hc:
            tr = await hc.post("https://id.twitch.tv/oauth2/token",
                               params={"client_id": cid, "client_secret": csec,
                                       "grant_type": "client_credentials"})
            tr.raise_for_status()
            token = tr.json()["access_token"]
            sr = await hc.get("https://api.twitch.tv/helix/streams",
                              params={"user_login": TWITCH_CHANNEL},
                              headers={"Client-Id": cid,
                                       "Authorization": f"Bearer {token}"})
            sr.raise_for_status()
            data = sr.json().get("data") or []
        if not data:
            return
        stream = data[0]
        stream_id = str(stream.get("id"))
        cur = await db.execute("SELECT 1 FROM golive_posts WHERE stream_id = ?",
                               (stream_id,))
        if await cur.fetchone():
            return
        cur = await db.execute("SELECT id FROM users WHERE username = 'jarvis' LIMIT 1")
        jrow = await cur.fetchone()
        if not jrow:
            return
        title = stream.get("title") or "live"
        game = stream.get("game_name") or "on Twitch"
        body = (f"LIVE NOW - {title} | {game}\n"
                f"Pull up: /watch #live")
        cur = await db.execute(
            "INSERT INTO posts (user_id, body, kind, created_at) VALUES (?, ?, 'post', ?)",
            (jrow["id"], body, _now()))
        pid = cur.lastrowid
        await _save_hashtags(db, pid, body)
        await db.execute(
            "INSERT INTO golive_posts (stream_id, post_id, created_at) VALUES (?, ?, ?)",
            (stream_id, pid, _now()))
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass


@app.get("/watch")
async def watch_page(request: Request, user=Depends(current_user),
                       db=Depends(get_db)):
    """Followed-streams grid: live thumbnails and offline cards for the
    configured channels. Clicking a card opens that channel's player view.
    Login-gated."""
    redir = login_required(user)
    if redir:
        return redir
    await maybe_post_golive(db)
    channels = await get_watch_channels_status(list(WATCH_CHANNELS))
    if not channels:
        # Twitch unreachable/unconfigured: still show cards, all offline.
        channels = [{"login": c, "display_name": c, "is_live": False,
                     "title": "", "game_name": "", "viewer_count": 0,
                     "thumbnail_url": "", "profile_image_url": "",
                     "offline_image_url": "", "started_at": ""}
                    for c in WATCH_CHANNELS]
    return templates.TemplateResponse(request, "watch.html",
        {"user": user, "channels": channels})


@app.get("/watch/{channel}")
async def watch_channel_page(request: Request, channel: str,
                             user=Depends(current_user)):
    """Player view for one followed channel: stream left (~3/4), chat right
    (~1/4); stacked on mobile. Login-gated."""
    redir = login_required(user)
    if redir:
        return redir
    login = (channel or "").strip().lower()
    if login not in WATCH_CHANNELS:
        return templates.TemplateResponse(request, "404.html",
                                          {"user": user}, status_code=404)
    player_url, chat_url = _watch_embeds(request, login)
    return templates.TemplateResponse(request, "watch_player.html",
        {"user": user, "channel": login,
         "player_url": player_url, "chat_url": chat_url})

@app.get("/picks")
async def picks_page(request: Request, user=Depends(current_user), msg: str = ""):
    """Budz Picks: Jarvis's daily picks & parlay tickets."""
    redir = login_required(user)
    if redir:
        return redir
    return templates.TemplateResponse(request, "picks.html",
                                      {"user": user, "msg": msg,
                                       "picks": load_budz_picks()})

