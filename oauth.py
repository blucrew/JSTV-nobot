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
    """JTV has no /me endpoint. /api/users/stream-settings returns the
    authenticated streamer's username, channel_id, and other identity fields."""
    url = f"{JTV_API_BASE}/users/stream-settings"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(url, headers=headers) as r:
            body = await r.text()
            if r.status != 200:
                raise RuntimeError(f"{url}→{r.status}: {body[:200]}")
            data = await r.json(content_type=None)
    # Normalise to a common shape the caller expects.
    # channel_id doubles as the unique user identifier when no separate id field exists.
    user_id = (
        str(data.get("id") or data.get("user_id") or data.get("channel_id") or "")
    )
    username = str(data.get("username") or data.get("name") or "").strip()
    if not user_id or not username:
        raise RuntimeError(f"stream-settings missing id/username: {data}")
    return {
        "id": user_id,
        "username": username,
        "channel_id": str(data.get("channel_id") or user_id),
    }
