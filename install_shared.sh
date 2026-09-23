#!/bin/bash
# BudzBook shared-host installer.
# Run from cPanel Cron Jobs as a one-shot (Terminal is disabled on this plan):
#   if [ ! -f ~/budzbook_install.done ]; then git clone -q -b jarvis/shared-host-install https://github.com/starcitykilla/budzbook.git ~/budzbook 2>/dev/null; bash ~/budzbook/install_shared.sh >> ~/budzbook_install.log 2>&1; echo "exit=$? $(date)" >> ~/budzbook_install.log; touch ~/budzbook_install.done; fi
# Then check ~/budzbook_install.log in File Manager for INSTALL_OK and delete
# the cron job. Safe to re-run any time: pulls latest code, reinstalls deps,
# rewrites the Passenger startup file, and verifies the app boots.
#
# PREREQUISITE: create the app first in cPanel "Setup Python App" with
# application root "budzbook" (this also creates the virtualenv we install
# into). Startup file: passenger_wsgi.py, entry point: application.
set -u

APP="$HOME/budzbook"
REPO_PATH="starcitykilla/budzbook.git"
BRANCH="jarvis/shared-host-install"
# Optional: pass a GitHub read-only token as $1 (or enter it when asked)
# if the repo is private. Never share the token in chat.
TOKEN="${1:-}"
if [ -z "$TOKEN" ] && [ ! -d "$APP/.git" ] && [ -t 0 ]; then
  echo -n "GitHub token (only needed while the repo is private; Enter to skip): "
  read -rs TOKEN; echo
fi
if [ -n "$TOKEN" ]; then
  REPO="https://x-access-token:${TOKEN}@github.com/${REPO_PATH}"
else
  REPO="https://github.com/${REPO_PATH}"
fi

echo "== BudzBook installer =="

# 1. Code ---------------------------------------------------------------
if [ -d "$APP/.git" ]; then
  echo "-- updating existing clone"
  git -C "$APP" fetch -q origin "$BRANCH" && git -C "$APP" checkout -q "$BRANCH" && git -C "$APP" pull -q --ff-only origin "$BRANCH"
else
  echo "-- cloning repo"
  git clone -q -b "$BRANCH" "$REPO" "$APP" || { echo "INSTALL_FAIL: git clone failed"; exit 1; }
fi
cd "$APP" || { echo "INSTALL_FAIL: no $APP"; exit 1; }

# 2. Python -------------------------------------------------------------
# Single-instance guard: the every-minute cron can overlap a long pip install.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$HOME/budzbook_install.lock"
  flock -n 9 || { echo "SKIPPED: another installer run in progress"; exit 3; }
fi

# Prefer the venv cPanel's Setup Python App created for this app root,
# so Passenger serves exactly what we install. The cron environment has
# no python3 on PATH, so use the venv's python directly when it exists.
# Fall back to a local venv (used only for the boot check) if the app
# isn't registered yet.
VENV=""
for d in "$HOME"/virtualenv/budzbook/*/; do
  if [ -x "${d}bin/python" ]; then VENV="$d"; break; fi
done
if [ -n "$VENV" ]; then
  PYBIN="$VENV/bin/python"
else
  PYBIN="$(command -v python3.12 || command -v python3.11 || command -v python3 || true)"
  [ -n "$PYBIN" ] || { echo "INSTALL_FAIL: no python3 found"; exit 1; }
  VENV="$APP/venv"
  [ -d "$VENV" ] || "$PYBIN" -m venv "$VENV" || { echo "INSTALL_FAIL: venv creation failed"; exit 1; }
fi
echo "-- python: $($PYBIN --version 2>&1) at $PYBIN"
echo "-- venv: $VENV"

# 3. Dependencies --------------------------------------------------------
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r requirements.txt a2wsgi || { echo "INSTALL_FAIL: pip install failed"; exit 1; }

# 4. Passenger startup file ----------------------------------------------
cat > passenger_wsgi.py <<'PYEOF'
import os, sys, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Passenger/WSGI never runs the ASGI lifespan hook, so init the DB here.
from app.db import init_db
asyncio.run(init_db())
# Bridge the async FastAPI app to WSGI for Passenger/mod_passenger.
from a2wsgi import ASGIMiddleware
from app.main import app as _asgi_app
application = ASGIMiddleware(_asgi_app)
PYEOF
echo "-- wrote passenger_wsgi.py"

# 5. Boot verification ----------------------------------------------------
if "$VENV/bin/python" -c "import passenger_wsgi; print('WSGI_OK')" 2>/tmp/budzbook_install_err.log | grep -q WSGI_OK; then
  echo "INSTALL_OK"
else
  echo "INSTALL_FAIL: app failed to import. First error lines:"
  head -20 /tmp/budzbook_install_err.log
  exit 1
fi

