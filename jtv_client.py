"""ActionCable WebSocket client for JoystickTV. Handshake is mandatory sequence:
  connect → welcome → subscribe → confirm_subscription → (now safe to send/recv).

Every one of the bulletpointed bugs below has bitten BluBot in the past:
  * Skipping welcome verification — works ~80% of the time until JTV restarts
  * Sending subscribe before welcome — server silently drops the subscribe
  * Starting senders before confirm_subscription — first outbound frames vanish
  * Ignoring 'disconnect' frames — server-initiated reconnect kills the bot
  * Silently dropping unknown event_name values — new events go unnoticed

This module gets all five right. Do not regress any of them."""
import asyncio
import contextlib
import json
import logging
import random
import ssl
from typing import Any, Awaitable, Callable, Optional

import base64
import websockets
import websockets.exceptions

from config import JOYSTICK_APP_ID, JOYSTICK_BOT_SECRET, JTV_WS_URL, RECONNECT_MIN, RECONNECT_MAX

# WS auth uses Application ID (not OAuth Client ID) + shared secret, Basic Auth encoded.
_WS_TOKEN = base64.b64encode(f"{JOYSTICK_APP_ID}:{JOYSTICK_BOT_SECRET}".encode()).decode()

# Explicit SSL context matching emojibuddy's proven approach.
_SSL = ssl.create_default_context()

log = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when the server signals that our token is invalid."""


def _make_identifier(channel_id: str) -> str:
    """Build the ActionCable subscription identifier string for a channel.
    Must be compact JSON with no extra whitespace — ActionCable compares
    identifiers as raw strings on the server side."""
    return json.dumps({"channel": "GatewayChannel", "id": str(channel_id)}, separators=(",", ":"))


class JtvClient:
    """Manages one streamer's WebSocket connection to JTV.

    Responsibilities:
      * Reconnect forever with exponential backoff
      * Perform the ActionCable handshake correctly every connect
      * Expose send_chat() / send_whisper() (queued; delivered after handshake)
      * Invoke on_event(event_name, data) for every received channel frame
    """

    def __init__(
        self,
        *,
        label: str,
        channel_id: str,
        on_event: Callable[[str, dict], Awaitable[None]],
    ) -> None:
        self.label = label
        self._identifier = _make_identifier(channel_id)
        self._on_event = on_event
        self._out_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._stopping = False
        self._ws: Optional[Any] = None
        self._subscribed = asyncio.Event()

    # --- public API ----------------------------------------------------------

    async def send_chat(self, text: str) -> None:
        await self._out_queue.put(
            {"action": "chat", "message": text}
        )

    async def send_whisper(self, target_user_id: str, text: str) -> None:
        await self._out_queue.put(
            {
                "action": "whisper",
                "target_user_id": str(target_user_id),
                "message": text,
            }
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    async def run(self) -> None:
        """Connect forever. Returns only when stop() is called."""
        backoff = RECONNECT_MIN
        while not self._stopping:
            try:
                await self._connect_and_handle()
                backoff = RECONNECT_MIN
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[%s] connection loop error", self.label)

            if self._stopping:
                break

            sleep_for = backoff + random.uniform(0, backoff * 0.3)
            log.info("[%s] reconnecting in %.1fs", self.label, sleep_for)
            await asyncio.sleep(sleep_for)
            backoff = min(backoff * 2, RECONNECT_MAX)

    # --- connection lifecycle -----------------------------------------------

    async def _connect_and_handle(self) -> None:
        url = f"{JTV_WS_URL}?token={_WS_TOKEN}"
        self._subscribed.clear()

        log.info("[%s] connecting", self.label)
        try:
            async with websockets.connect(
                url,
                ssl=_SSL,
                subprotocols=["actioncable-v1-json"],
                open_timeout=20,
                ping_interval=25,
                ping_timeout=20,
                max_size=4 * 1024 * 1024,
            ) as ws:
                self._ws = ws
                try:
                    await self._await_welcome(ws)
                    await ws.send(
                        json.dumps({"command": "subscribe", "identifier": self._identifier})
                    )
                    await self._await_confirm(ws)

                    log.info("[%s] subscribed; entering main loop", self.label)
                    self._subscribed.set()
                    sender_task = asyncio.create_task(self._sender_loop(ws))
                    try:
                        await self._receive_loop(ws)
                    finally:
                        sender_task.cancel()
                        with contextlib.suppress(Exception, asyncio.CancelledError):
                            await sender_task
                finally:
                    self._ws = None
                    self._subscribed.clear()
        except websockets.exceptions.InvalidHandshake as e:
            log.error("[%s] WS handshake rejected (HTTP-level): %s", self.label, e)
            raise

    # --- handshake helpers ---------------------------------------------------

    async def _await_welcome(self, ws) -> None:
        """Block until we see {'type':'welcome'}. ConnectionClosed before it → bail."""
        while True:
            try:
                data = await ws.recv()
            except websockets.exceptions.ConnectionClosed as e:
                reason = getattr(e, "reason", "") or str(e)
                if any(w in str(reason).lower() for w in ("unauthorized", "invalid")):
                    raise AuthError(reason)
                raise RuntimeError(f"[{self.label}] closed before welcome: {e}")
            if not isinstance(data, str):
                continue
            try:
                frame = json.loads(data)
            except json.JSONDecodeError:
                continue
            t = frame.get("type")
            if t == "welcome":
                log.debug("[%s] got welcome", self.label)
                return
            if t == "disconnect":
                reason = frame.get("reason", "")
                if reason in ("unauthorized", "invalid_request"):
                    raise AuthError(reason)
                raise RuntimeError(f"[{self.label}] disconnect before welcome: {reason}")
            if t == "ping":
                continue
            log.debug("[%s] pre-welcome frame: %s", self.label, frame)

    async def _await_confirm(self, ws) -> None:
        while True:
            try:
                data = await ws.recv()
            except websockets.exceptions.ConnectionClosed as e:
                raise RuntimeError(f"[{self.label}] closed before confirm: {e}")
            if not isinstance(data, str):
                continue
            try:
                frame = json.loads(data)
            except json.JSONDecodeError:
                continue
            t = frame.get("type")
            if t == "confirm_subscription":
                log.debug("[%s] confirm_subscription", self.label)
                return
            if t == "reject_subscription":
                raise RuntimeError(f"[{self.label}] subscription rejected")
            if t == "disconnect":
                reason = frame.get("reason", "")
                if reason in ("unauthorized", "invalid_request"):
                    raise AuthError(reason)
                raise RuntimeError(f"[{self.label}] disconnect before confirm: {reason}")
            if t == "ping":
                continue
            log.debug("[%s] pre-confirm frame: %s", self.label, frame)

    async def _receive_loop(self, ws) -> None:
        try:
            async for data in ws:
                if not isinstance(data, str):
                    continue
                try:
                    frame = json.loads(data)
                except json.JSONDecodeError:
                    log.debug("[%s] non-JSON frame: %r", self.label, data)
                    continue
                await self._dispatch(frame)
        except websockets.exceptions.ConnectionClosed as e:
            log.info("[%s] ws closed: %s", self.label, e)

    async def _dispatch(self, frame: dict) -> None:
        t = frame.get("type")
        if t == "ping":
            return
        if t == "disconnect":
            reason = frame.get("reason", "")
            log.warning("[%s] server disconnect: %s", self.label, reason)
            if reason in ("unauthorized", "invalid_request"):
                raise AuthError(reason)
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.close()
            return
        if t in ("welcome", "confirm_subscription", "reject_subscription"):
            log.debug("[%s] late handshake frame: %s", self.label, t)
            return
        if "message" in frame and frame.get("identifier") == self._identifier:
            payload = frame["message"]
            if not isinstance(payload, dict):
                return
            event_name = payload.get("event_name") or payload.get("event") or ""
            data = payload.get("data")
            if data is None:
                data = {k: v for k, v in payload.items() if k not in ("event_name", "event")}
            try:
                await self._on_event(event_name, data if isinstance(data, dict) else {"_raw": data})
            except Exception:
                log.exception("[%s] on_event handler crashed for %s", self.label, event_name)
            return
        log.debug("[%s] unhandled frame: %s", self.label, frame)

    async def _sender_loop(self, ws) -> None:
        """Pop outbound items and write them. Only starts after confirm_subscription."""
        while True:
            item = await self._out_queue.get()
            try:
                frame = {
                    "command": "message",
                    "identifier": self._identifier,
                    "data": json.dumps(item),
                }
                await ws.send(json.dumps(frame))
            except Exception:
                log.exception("[%s] failed to send frame, requeueing", self.label)
                await self._out_queue.put(item)
                return
