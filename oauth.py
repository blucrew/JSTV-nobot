"""OAuth install flow: /install kicks off JTV authorization, /auth/callback
exchanges the code, upserts the streamer, and spawns their live session.
"""
import logging
import time
from urllib.parse import urlencode

import aiohttp
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import db
from config import (
    JOYSTICK_BOT_ID,
    JOYSTICK_BOT_SECRET,
    JTV_API_BASE,
    JTV_AUTHORIZE_URL,
    JTV_TOKEN_URL,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPES,
    ROOT_PATH,
    panel_url,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix=ROOT_PATH)
templates = Jinja2Templates(directory="templates")


def _render(request: Request, template: str, context: dict, status_code: int = 200):
    """Starlette 1.0 TemplateResponse: request is first arg, context is separate."""
    return templates.TemplateResponse(
        request, template, context | {"root_path": ROOT_PATH}, status_code=status_code
    )


@router.get("/")
async def root(request: Request) -> HTMLResponse:
    return _render(request, "landing.html", {"install_url": f"{ROOT_PATH}/install"})


@router.get("/install")
async def install() -> RedirectResponse:
    state = db.new_oauth_state()
    params = {
        "response_type": "code",
        "client_id": JOYSTICK_BOT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": OAUTH_SCOPES,
        "state": state,
    }
    return RedirectResponse(f"{JTV_AUTHORIZE_URL}?{urlencode(params)}")


@router.get("/auth/callback")
async def callback(request: Request) -> RedirectResponse:
    qp = request.query_params
    error = qp.get("error")
    code = qp.get("code")
    state = qp.get("state")

    if error:
        log.warning("oauth callback error: %s", error)
        return _render(
            request, "error.html",
            {"title": "Install cancelled", "message": f"JoystickTV returned: {error}"},
            status_code=400,
        )
    if not code:
        raise HTTPException(400, "missing code")
    # State is CSRF protection for our own /install flow. JTV's marketplace
    # install bypasses our /install page and sends state="" — skip validation
    # in that case rather than rejecting legitimate marketplace installs.
    if state:
        if not db.consume_oauth_state(state):
            raise HTTPException(400, "invalid or expired state")

    try:
        token_data = await _exchange_code(code)
    except Exception as e:
        log.exception("token exchange failed")
        return _render(
            request, "error.html",
            {"title": "Install failed", "message": f"Token exchange failed: {e}"},
            status_code=502,
        )

    try:
        me = await _fetch_identity(token_data["access_token"])
    except Exception as e:
        log.exception("identity fetch failed")
        return _render(
            request, "error.html",
            {"title": "Install failed", "message": f"Could not read your JTV account: {e}"},
            status_code=502,
        )

    jtv_user_id = str(me.get("id") or me.get("user_id") or "")
    jtv_username = str(me.get("username") or me.get("name") or "").strip()
    if not jtv_user_id or not jtv_username:
        raise HTTPException(502, "JTV identity response missing id/username")

    streamer = db.upsert_streamer(
        jtv_user_id=jtv_user_id,
        jtv_username=jtv_username,
        channel_id=str(me.get("channel_id") or me.get("channel") or "") or None,
        access_token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token", ""),
        token_expires_at=_parse_expiry(token_data.get("expires_in")),
    )

    supervisor = request.app.state.supervisor
    await supervisor.spawn(streamer)

    log.info("installed: %s (id=%s, slug=%s)", jtv_username, streamer.id, streamer.panel_slug)
    return RedirectResponse(
        panel_url(streamer.panel_slug, streamer.panel_token) + "&welcome=1",
        status_code=303,
    )


def _parse_expiry(expires_in) -> int:
    """JTV returns expires_in as an absolute Unix timestamp, not seconds.
    Guard against either format defensively."""
    now = int(time.time())
    try:
        v = int(expires_in)
    except (TypeError, ValueError):
        return now + 3600
    # If the value is plausibly a future timestamp (> year 2020), use directly.
    return v if v > 1_000_000_000 else now + v


async def _fetch_username(access_token: str, fallback: str) -> str:
    """Best-effort username lookup — never raises, always returns something."""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(f"{JTV_API_BASE}/users/me", headers=headers) as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    # Response may be nested under 'data' with a 'slug' field
                    username = (
                        (d.get("data") or {}).get("slug")
                        or (d.get("data") or {}).get("username")
                        or d.get("username")
                        or d.get("slug")
                        or d.get("name")
                        or ""
                    )
                    if username:
                        log.info("resolved username: %s", username)
                        return str(username)
                log.info("users/me returned %s — using channel_id as username", r.status)
    except Exception as e:
        log.info("users/me fetch failed (%s) — using channel_id as username", e)
    return fallback


async def _exchange_code(code: str) -> dict:
    # JTV token exchange: Basic Auth header, params as query string (not body).
    # ref: https://support.joystick.tv/developer_support/#fetching-access_token-api
    params = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": OAUTH_REDIRECT_URI,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    auth = aiohttp.BasicAuth(JOYSTICK_BOT_ID, JOYSTICK_BOT_SECRET)
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(JTV_TOKEN_URL, params=params, headers=headers, auth=auth) as r:
            body = await r.text()
            if r.status != 200:
                raise RuntimeError(f"{r.status}: {body[:200]}")
            return await r.json(content_type=None)


async def _fetch_identity(access_token: str) -> dict:
    """Decode the JWT payload locally — no API call, no extra permissions.
    JTV issues a signed JWT as the access_token; the payload contains the
    streamer's identity claims."""
    import base64, json as _json
    parts = access_token.split(".")
    if len(parts) != 3:
        raise RuntimeError("access_token is not a JWT")
    # base64url → bytes (add padding as needed)
    padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
    try:
        claims = _json.loads(base64.urlsafe_b64decode(padded))
    except Exception as e:
        raise RuntimeError(f"JWT payload decode failed: {e}")

    log.info("JWT claims: %s", claims)

    channel_id = str(claims.get("channel_id") or "")
    if not channel_id:
        raise RuntimeError(f"JWT missing channel_id — full claims: {claims}")

    # Try to resolve the streamer's display username. Falls back to channel_id
    # as a placeholder (same pattern as emojibuddy) — won't block install.
    username = await _fetch_username(access_token, channel_id)
    return {"id": channel_id, "username": username, "channel_id": channel_id}
