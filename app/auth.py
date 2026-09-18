"""Password hashing and cookie sessions for BudzBook.

Sessions are signed (not encrypted) cookies via itsdangerous: the cookie
holds ``{"uid": <user_id>}`` and the signature proves we issued it. If the
secret ever changes, all sessions are invalidated (everyone logs in again).

.. warning::
    ``SESSION_SECRET`` below is a hard-coded DEV constant. That is fine for
    local v1, but for anything public it MUST come from an environment
    variable / secret manager instead. Never commit a real secret here.
"""
from itsdangerous import BadSignature, URLSafeSerializer
from passlib.context import CryptContext
import os
from datetime import datetime, timezone

# Sessions are signed with this secret. Set THC_SESSION_SECRET in the
# environment for anything public; the hard-coded value is dev-only.
# Changing it invalidates all existing login sessions.
SESSION_SECRET = os.environ.get("THC_SESSION_SECRET", "thc-dev-secret-CHANGE-ME-IN-PRODUCTION")
SESSION_COOKIE = "thc_session"

_serializer = URLSafeSerializer(SESSION_SECRET, salt="thc-social-session")
# Age-gate tokens use their own salt so a session cookie can never double
# as an age pass (and vice versa).
_age_serializer = URLSafeSerializer(SESSION_SECRET, salt="thc-age-gate")
AGE_COOKIE = "thc_age"
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _pwd.verify(password, password_hash)


def make_session_token(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id})


def read_session_token(token: str):
    """Return the user id from a session cookie, or None if invalid."""
    try:
        data = _serializer.loads(token)
    except BadSignature:
        return None
    uid = data.get("uid")
    return uid if isinstance(uid, int) else None


def make_age_token() -> str:
    """Signed 21+ pass for the age gate. Stored in the AGE_COOKIE."""
    return _age_serializer.dumps({
        "age_ok": True,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


def read_age_token(token: str) -> bool:
    """True only for a token we actually issued as an age pass."""
    if not token:
        return False
    try:
        data = _age_serializer.loads(token)
    except BadSignature:
        return False
    return data.get("age_ok") is True


# Staged Twitch OAuth profiles awaiting Terms acceptance. A new OAuth user
# is not created until they accept the Terms; their Twitch profile rides in
# this signed token (separate salt so it can never double as a session or
# age pass). Short-lived-ish: 30-minute expiry.
_pending_serializer = URLSafeSerializer(SESSION_SECRET, salt="thc-terms-pending")
_PENDING_TTL_SECONDS = 30 * 60


def make_pending_oauth_token(profile: dict) -> str:
    """Sign a staged OAuth profile dict for the /terms interstitial."""
    return _pending_serializer.dumps({
        "profile": profile,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


def read_pending_oauth_token(token: str):
    """Return the staged profile dict, or None if invalid/expired."""
    if not token:
        return None
    try:
        data = _pending_serializer.loads(token)
    except BadSignature:
        return None
    try:
        ts = datetime.fromisoformat(data.get("ts", ""))
    except (ValueError, TypeError):
        return None
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age < 0 or age > _PENDING_TTL_SECONDS:
        return None
    profile = data.get("profile")
    return profile if isinstance(profile, dict) else None
