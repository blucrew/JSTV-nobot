"""Per-streamer session: owns a JtvClient, handles inbound chat, manages the
`nobot_enabled` toggle (in-memory, resets to True on stream go-live), and polls
/users/stream-settings to detect live transitions + drive token refresh.
"""
import asyncio
import contextlib
import logging
import time
from typing import Optional

import aiohttp

import db
import no_cache
from config import (
    BOT_REPLY_MARKER,
    BOT_USERNAME,
    JOYSTICK_BOT_ID,
    JOYSTICK_BOT_SECRET,
    JTV_STREAM_SETTINGS_URL,
    JTV_TOKEN_URL,
    STREAM_POLL_INTERVAL,
    panel_url,
)
from detector import is_simple_no
from jtv_client import JtvClient

log = logging.getLogger(__name__)


def _parse_expiry(expires_in) -> int:
    now = int(time.time())
    try:
        v = int(expires_in)
    except (TypeError, ValueError):
        return now + 3600
    return v if v > 1_000_000_000 else now + v


async def refresh_tokens(streamer: db.Streamer) -> None:
    """Exchange refresh_token for a fresh access_token. Persists to DB."""
    params = {
        "grant_type": "refresh_token",
        "refresh_token": streamer.refresh_token,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    auth = aiohttp.BasicAuth(JOYSTICK_BOT_ID, JOYSTICK_BOT_SECRET)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.post(JTV_TOKEN_URL, params=params, headers=headers, auth=auth) as r:
            body = await r.text()
            if r.status != 200:
                raise RuntimeError(
                    f"refresh_token failed {r.status}: {body[:200]}"
                )
            tok = await r.json(content_type=None)
    access = tok["access_token"]
    refresh = tok.get("refresh_token", streamer.refresh_token)
    db.update_tokens(
        streamer.id, access, refresh, _parse_expiry(tok.get("expires_in"))
    )
    log.info("[%s] tokens refreshed", streamer.jtv_username)


class StreamerSession:
    """One live session for one streamer. Kept alive by the supervisor."""

    def __init__(self, streamer: db.Streamer) -> None:
        self.streamer = streamer
        # nobot_enabled starts True; reset to True whenever stream goes live.
        self.nobot_enabled: bool = True
        self.last_is_live: Optional[bool] = None
        self.client = JtvClient(
            label=streamer.jtv_username,
            get_access_token=self._current_access_token,
            refresh_access_token=self._refresh_and_reload,
            on_event=self._on_event,
        )
        self._tasks: list[asyncio.Task] = []

    # --- public API ----------------------------------------------------------

    async def run(self) -> None:
        ws_task = asyncio.create_task(self.client.run(), name=f"ws-{self.streamer.id}")
        poll_task = asyncio.create_task(self._poll_loop(), name=f"poll-{self.streamer.id}")
        self._tasks = [ws_task, poll_task]
        try:
            done, pending = await asyncio.wait(
                self._tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            for t in pending:
                t.cancel()
            for t in pending:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await t
            # Surface the first exception if any
            for t in done:
                exc = t.exception()
                if exc:
                    raise exc
        finally:
            await self.client.stop()

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    async def fire_no(self) -> None:
        """Send one 'no' phrase to chat. Bypasses toggle and cooldowns."""
        phrase = no_cache.cache.get()
        await self.client.send_chat(BOT_REPLY_MARKER + phrase)

    # --- event handling ------------------------------------------------------

    async def _on_event(self, event_name: str, data: dict) -> None:
        if event_name == "ChatMessage":
            await self._on_chat(data)
            return
        if event_name in (
            "StreamEvent",
            "UserPresence",
            "ViewerCountUpdated",
            "FollowerCountUpdated",
            "SubscriberCountUpdated",
        ):
            # v1 doesn't act on these but we log at DEBUG so anything unusual
            # shows up if we turn up verbosity.
            log.debug("[%s] %s %s", self.streamer.jtv_username, event_name, data)
            return
        # Unknown event — log, don't silently drop (BluBot lesson).
        log.debug("[%s] unknown event_name %r data=%s",
                  self.streamer.jtv_username, event_name, data)

    async def _on_chat(self, data: dict) -> None:
        sender = data.get("user") if isinstance(data.get("user"), dict) else data
        username = (sender.get("username") or "").lower() if isinstance(sender, dict) else ""
        text = (
            data.get("message")
            or data.get("body")
            or data.get("text")
            or ""
        )
        if not isinstance(text, str) or not text.strip():
            return

        # Self-filter: skip the bot's own messages (primary: username; fallback:
        # zero-width marker we prefix on all our outbound).
        if username and username == BOT_USERNAME:
            return
        if text.startswith(BOT_REPLY_MARKER):
            return

        if not self._is_privileged(data, sender):
            return

        stripped = text.strip()
        low = stripped.lower()

        # Command routing (match before refusal detection)
        if low in ("!nobot on", "!nobot enable"):
            self.nobot_enabled = True
            await self.client.send_chat(
                BOT_REPLY_MARKER + "noBot is now ON for this stream."
            )
            return
        if low in ("!nobot off", "!nobot disable"):
            self.nobot_enabled = False
            await self.client.send_chat(
                BOT_REPLY_MARKER + "noBot is now OFF for this stream."
            )
            return
        if low == "!nobothelp":
            await self._send_help_whisper(sender if isinstance(sender, dict) else {})
            return

        if not self.nobot_enabled:
            return
        if is_simple_no(stripped):
            await self.fire_no()

    def _is_privileged(self, data: dict, sender: dict) -> bool:
        """Return True iff sender is the streamer or a moderator.

        Defensive: JTV payload shape isn't fully nailed down from the brief, so
        we check several plausible fields and fall back to a direct match on
        the streamer's own JTV user id / username.
        """
        if not isinstance(sender, dict):
            sender = {}
        user_id = str(sender.get("id") or "")
        username = (sender.get("username") or "").lower()

        if user_id and user_id == str(self.streamer.jtv_user_id):
            return True
        if username and username == self.streamer.jtv_username.lower():
            return True
        if sender.get("is_streamer") or sender.get("is_moderator"):
            return True
        if data.get("is_streamer") or data.get("is_moderator"):
            return True

        roles = sender.get("roles") or data.get("roles")
        if isinstance(roles, list):
            for r in roles:
                if str(r).lower() in ("streamer", "moderator", "mod", "owner"):
                    return True

        role = str(sender.get("role") or data.get("role") or "").lower()
        if role in ("streamer", "moderator", "mod", "owner"):
            return True
        return False

    async def _send_help_whisper(self, sender: dict) -> None:
        target_id = str(sender.get("id") or "")
        url = self._panel_url()
        msg = (
            f"noBot commands: '!nobot on' / '!nobot off' toggle the chat "
            f"trigger (streamer and mods only; resets to ON when you go live). "
            f"Your private trigger panel: {url} — bookmark it or add as an "
            f"OBS browser source dock."
        )
        if target_id:
            await self.client.send_whisper(target_id, BOT_REPLY_MARKER + msg)
        else:
            log.warning(
                "[%s] !nobothelp invoked without sender id — skipping",
                self.streamer.jtv_username,
            )

    def _panel_url(self) -> str:
        return panel_url(self.streamer.panel_slug, self.streamer.panel_token)

    # --- token & polling -----------------------------------------------------

    async def _current_access_token(self) -> str:
        fresh = db.get_streamer_by_id(self.streamer.id)
        if fresh:
            self.streamer = fresh
        return self.streamer.access_token

    async def _refresh_and_reload(self) -> None:
        await refresh_tokens(self.streamer)
        fresh = db.get_streamer_by_id(self.streamer.id)
        if fresh:
            self.streamer = fresh

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(STREAM_POLL_INTERVAL)
                await self._poll_stream_settings()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("[%s] stream-settings poll error",
                              self.streamer.jtv_username)

    async def _poll_stream_settings(self) -> None:
        token = await self._current_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(JTV_STREAM_SETTINGS_URL, headers=headers) as r:
                if r.status == 401:
                    log.info("[%s] stream-settings 401, refreshing token",
                             self.streamer.jtv_username)
                    try:
                        await self._refresh_and_reload()
                    except Exception:
                        log.exception("[%s] proactive refresh failed",
                                      self.streamer.jtv_username)
                    return
                if r.status != 200:
                    log.debug("[%s] stream-settings %s",
                              self.streamer.jtv_username, r.status)
                    return
                data = await r.json(content_type=None)

        is_live = bool(
            data.get("is_live")
            or data.get("live")
            or data.get("online")
        )
        # Flip enabled→True on offline→live transition (per spec: "state resets
        # to on every time the stream goes live").
        if self.last_is_live is False and is_live:
            log.info("[%s] stream went live → resetting noBot to ON",
                     self.streamer.jtv_username)
            self.nobot_enabled = True
        self.last_is_live = is_live
