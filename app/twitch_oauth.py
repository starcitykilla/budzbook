"""Twitch OAuth login for BudzBook.

Lets viewers log in (or link their existing BudzBook account) with their
Twitch identity. The verified Twitch user ID is stored as users.twitch_id
and is the authoritative link between a Twitch viewer and their BudzBook
account — wallet payouts match on it first.

One-time setup (Brad):
  1. Create an app at https://dev.twitch.tv/console (OAuth Redirect URLs)
  2. Add redirect URL: http://localhost:8000/auth/twitch/callback
     (later also: https://budzbook.us/auth/twitch/callback)
  3. Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in the environment
     before starting the server. Without them the Twitch button is hidden
     and /auth/twitch redirects back to /login.
"""
import os
import secrets
import urllib.parse

import httpx
from itsdangerous import BadSignature, URLSafeSerializer

from .auth import SESSION_SECRET  # reuse the session secret for state signing

def _client_id() -> str:
    return os.environ.get("TWITCH_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_USERS_URL = "https://api.twitch.tv/helix/users"
# Minimal scopes: identity + email only. No chat or moderation access.
TWITCH_SCOPES = ["user:read:email"]

_state = URLSafeSerializer(SESSION_SECRET, salt="budzbook-twitch-oauth")


def configured() -> bool:
    """True when the Twitch app credentials are present."""
    return bool(_client_id() and _client_secret())


def make_state(link_uid=None) -> str:
    """Signed CSRF state for the authorize redirect (10-minute lifetime)."""
    return _state.dumps({"nonce": secrets.token_hex(8), "link_uid": link_uid})


def read_state(token: str):
    """Return the state dict, or None when missing/tampered/expired."""
    try:
        data = _state.loads(token, max_age=600)
    except BadSignature:
        return None
    if not isinstance(data, dict) or "nonce" not in data:
        return None
    return data


def authorize_url(redirect_uri: str, state: str) -> str:
    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(TWITCH_SCOPES),
        "state": state,
    }
    return TWITCH_AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


async def exchange_code(code: str, redirect_uri: str) -> str:
    """Exchange the authorize code for a user access token. Raises on failure."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            TWITCH_TOKEN_URL,
            data={
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def fetch_twitch_user(access_token: str) -> dict:
    """Return the Twitch user record (id, login, display_name, ...). Raises."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            TWITCH_USERS_URL,
            headers={
                "Client-Id": _client_id(),
                "Authorization": f"Bearer {access_token}",
            },
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
        if not data:
            raise ValueError("Twitch returned no user")
        return data[0]
