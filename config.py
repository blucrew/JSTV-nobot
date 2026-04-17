"""Environment-var authoritative config. No real-value defaults for secrets."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"[FATAL] Missing required env var: {name}", file=sys.stderr)
        sys.exit(1)
    return v


# Required
JOYSTICK_BOT_ID = _require("JOYSTICK_BOT_ID")
JOYSTICK_BOT_SECRET = _require("JOYSTICK_BOT_SECRET")
BOT_USERNAME = _require("BOT_USERNAME").lower()
PUBLIC_BASE_URL = _require("PUBLIC_BASE_URL").rstrip("/")
APP_SECRET = _require("APP_SECRET")

# Optional with sensible defaults
DB_PATH = Path(os.environ.get("DB_PATH", "nobot.db"))
NAAS_URL = os.environ.get("NAAS_URL", "https://naas.isalman.dev/no").strip()
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))

# Path prefix the reverse proxy forwards to us. On the shared droplet this
# lets multiple bots coexist at different prefixes (e.g. /emojibuddy, /nobot).
# Leave empty for standalone deployment. Do NOT include trailing slash.
ROOT_PATH = os.environ.get("ROOT_PATH", "").rstrip("/")

# JTV endpoints (per developer docs)
JTV_AUTHORIZE_URL = "https://joystick.tv/api/oauth/authorize"
JTV_TOKEN_URL = "https://joystick.tv/api/oauth/token"
JTV_WS_URL = "wss://joystick.tv/cable"
JTV_API_BASE = "https://joystick.tv/api"
JTV_STREAM_SETTINGS_URL = f"{JTV_API_BASE}/users/stream-settings"

# Derived — both the redirect and panel URLs include ROOT_PATH so they work
# correctly behind a path-prefix reverse proxy.
OAUTH_REDIRECT_URI = f"{PUBLIC_BASE_URL}{ROOT_PATH}/auth/callback"


def panel_url(slug: str, token: str) -> str:
    return f"{PUBLIC_BASE_URL}{ROOT_PATH}/panel/{slug}?k={token}"

# OAuth scopes we need — adjust once confirmed against JTV docs
OAUTH_SCOPES = "read write"

# Zero-width space prefix on bot replies. If we ever read our own message back,
# the detector skips anything starting with this even if sender-name matching fails.
BOT_REPLY_MARKER = "\u200b"

# How often to poll /users/stream-settings per streamer (seconds)
STREAM_POLL_INTERVAL = 120

# Reconnect backoff bounds (seconds)
RECONNECT_MIN = 2.0
RECONNECT_MAX = 60.0
