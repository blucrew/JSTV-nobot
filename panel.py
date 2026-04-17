"""Streamer's private panel: big red 'No' button + chat-command/OBS-dock help.

Auth model: bookmarkable signed URL. The slug is the lookup key, the k param
is a long random per-streamer token that must match what we stored. No cookie,
no login flow — OBS browser sources can't do OAuth, and the streamer can
rotate the token if the URL ever leaks.
"""
import hmac
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import db
from config import ROOT_PATH, panel_url

log = logging.getLogger(__name__)

router = APIRouter(prefix=ROOT_PATH)
templates = Jinja2Templates(directory="templates")


def _authorize(slug: str, provided_token: Optional[str]) -> db.Streamer:
    streamer = db.get_streamer_by_slug(slug)
    if not streamer or not provided_token:
        raise HTTPException(404, "panel not found")
    # Constant-time compare so timing-leaks can't be used to brute the token
    if not hmac.compare_digest(streamer.panel_token, provided_token):
        raise HTTPException(404, "panel not found")
    return streamer


@router.get("/panel/{slug}")
async def panel_page(slug: str, request: Request) -> HTMLResponse:
    k = request.query_params.get("k")
    streamer = _authorize(slug, k)
    welcome = request.query_params.get("welcome") == "1"

    return templates.TemplateResponse(
        "panel.html",
        {
            "request": request,
            "streamer_username": streamer.jtv_username,
            "panel_slug": streamer.panel_slug,
            "panel_token": streamer.panel_token,
            "panel_url": panel_url(streamer.panel_slug, streamer.panel_token),
            "fire_path": f"{ROOT_PATH}/panel/{streamer.panel_slug}/fire",
            "welcome": welcome,
            "root_path": ROOT_PATH,
        },
    )


@router.post("/panel/{slug}/fire")
async def panel_fire(slug: str, request: Request) -> JSONResponse:
    """Fires one 'no' phrase into the streamer's chat. Always fires. Bypasses
    the !nobot toggle and any cooldown — per spec, this button fires every
    time the streamer presses it."""
    k = request.query_params.get("k")
    streamer = _authorize(slug, k)

    supervisor = request.app.state.supervisor
    session = supervisor.get_session(streamer.id)
    if not session:
        # Session isn't running — try to spawn (e.g. if the process just
        # started). If the connection hasn't finished the handshake yet, the
        # message will be queued and sent as soon as it's ready.
        await supervisor.spawn(streamer)
        session = supervisor.get_session(streamer.id)
        if not session:
            raise HTTPException(503, "bot is not running for this streamer")

    await session.fire_no()
    return JSONResponse({"ok": True})


@router.post("/panel/{slug}/rotate")
async def panel_rotate(slug: str, request: Request) -> JSONResponse:
    """Rotate the panel token — invalidates the current bookmarkable URL and
    returns a fresh one. Call this if the URL leaks."""
    k = request.query_params.get("k")
    streamer = _authorize(slug, k)
    new_token = db.rotate_panel_token(streamer.id)
    return JSONResponse(
        {"ok": True, "panel_url": panel_url(streamer.panel_slug, new_token)}
    )
