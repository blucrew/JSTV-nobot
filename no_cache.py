"""Background fetcher for no-as-a-service. Keeps a ring buffer topped up so
we never block a response on the external API being slow or down.

If both the API and the cache are empty, we serve from a hardcoded fallback list
so the bot stays functional on day one / during extended outages.
"""
import asyncio
import logging
import random
from collections import deque
from typing import Optional

import aiohttp

from config import NAAS_URL

log = logging.getLogger(__name__)

# Target pool size. The no-as-a-service endpoint returns one phrase per call,
# so we top up slowly over time.
_TARGET_SIZE = 500
# Top up this many per refresh tick (don't hammer their API on boot)
_BATCH_SIZE = 20
# Seconds between top-up ticks
_REFRESH_INTERVAL = 1800  # 30 min

# Hardcoded fallback — used only if cache is empty AND API is unreachable.
_FALLBACK: list[str] = [
    "No.",
    "Absolutely not.",
    "Nope.",
    "Hard pass.",
    "Negative.",
    "That's a no from me.",
    "Not in this lifetime.",
    "Not happening.",
    "No way, José.",
    "Denied.",
    "Under no circumstances.",
    "Nah.",
    "I'd rather not.",
    "No thanks.",
    "Not today.",
    "Not a chance.",
    "Pass.",
    "Out of the question.",
    "No can do.",
    "Nope, nope, nope.",
    "In your dreams.",
    "Heck no.",
    "Nuh uh.",
    "Zero. None. Negative.",
    "That ship has sailed.",
    "Ask me again and the answer gets shorter.",
    "I refuse.",
    "Not gonna happen.",
    "Don't think so.",
    "Nope. Next question.",
    "Not even close.",
    "Over my dead body.",
    "Hard no.",
    "Big no.",
    "Extra large no.",
    "No, with a side of no.",
    "I'd sooner eat a sock.",
    "Computer says no.",
    "Return to sender.",
    "Declined.",
    "Veto.",
    "No, and that's final.",
    "Not on my watch.",
    "Sorry, not sorry — no.",
    "Nope, try again never.",
    "N. O.",
    "That's going to be a no.",
    "Respectfully, no.",
    "Absolutely, positively, definitely not.",
    "No. Full stop.",
]


class NoCache:
    def __init__(self) -> None:
        self._buffer: deque[str] = deque(maxlen=_TARGET_SIZE)
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        )
        # Do an initial burst so we're not cold at first trigger
        await self._topup(_BATCH_SIZE)
        asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_REFRESH_INTERVAL)
                need = _TARGET_SIZE - len(self._buffer)
                if need > 0:
                    await self._topup(min(need, _BATCH_SIZE))
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("no-cache refresh loop error")

    async def _fetch_one(self) -> Optional[str]:
        if not self._session:
            return None
        try:
            async with self._session.get(NAAS_URL) as r:
                if r.status != 200:
                    log.warning("naas %s returned %s", NAAS_URL, r.status)
                    return None
                data = await r.json(content_type=None)
                # The upstream repo returns {"reason": "..."}; be defensive
                # against schema drift.
                for key in ("reason", "no", "message", "text"):
                    v = data.get(key) if isinstance(data, dict) else None
                    if isinstance(v, str) and v.strip():
                        return v.strip()
                if isinstance(data, str) and data.strip():
                    return data.strip()
                log.warning("naas unexpected payload: %r", data)
                return None
        except Exception as e:
            log.warning("naas fetch failed: %s", e)
            return None

    async def _topup(self, n: int) -> None:
        async with self._lock:
            fetched = 0
            for _ in range(n):
                phrase = await self._fetch_one()
                if phrase:
                    self._buffer.append(phrase)
                    fetched += 1
                await asyncio.sleep(0.1)  # be polite
            if fetched:
                log.info("no-cache topped up with %d phrases (size=%d)", fetched, len(self._buffer))

    def get(self) -> str:
        """Pop one cached phrase. Falls back to built-in list if empty.

        Uses popleft so we cycle through the buffer rather than returning the
        same recent phrase repeatedly; the refresh loop replenishes over time.
        """
        if self._buffer:
            return self._buffer.popleft()
        return random.choice(_FALLBACK)


# Module-level singleton used by the rest of the app
cache = NoCache()
