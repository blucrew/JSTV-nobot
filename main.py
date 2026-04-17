"""Entry point. Starts the FastAPI app, the no-phrase cache, and a supervisor
that keeps one StreamerSession alive per installed streamer.
"""
import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import db
import no_cache
import oauth
import panel
from bot import StreamerSession
from config import HOST, PORT, ROOT_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("nobot")


class Supervisor:
    """Keeps one StreamerSession alive per installed streamer.

    Callers hit .spawn(streamer) after install or at startup; it's idempotent
    and safe to call repeatedly. Crashed tasks auto-restart via an inner loop.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, StreamerSession] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._stopping = False

    def get_session(self, streamer_id: int) -> Optional[StreamerSession]:
        return self._sessions.get(streamer_id)

    async def spawn(self, streamer: db.Streamer) -> None:
        async with self._lock:
            task = self._tasks.get(streamer.id)
            if task and not task.done():
                # Already running. If the caller just re-installed (tokens
                # updated), the session re-reads them from DB on next use.
                return
            session = StreamerSession(streamer)
            self._sessions[streamer.id] = session
            self._tasks[streamer.id] = asyncio.create_task(
                self._supervise_one(session),
                name=f"supervisor-{streamer.id}",
            )

    async def spawn_all(self) -> None:
        for s in db.all_streamers():
            await self.spawn(s)

    async def _supervise_one(self, session: StreamerSession) -> None:
        """Run one session, auto-restart on crash with a short delay."""
        sid = session.streamer.id
        label = session.streamer.jtv_username
        while not self._stopping:
            try:
                await session.run()
                log.info("[%s] session exited cleanly", label)
                return
            except asyncio.CancelledError:
                log.info("[%s] session cancelled", label)
                raise
            except Exception:
                log.exception("[%s] session crashed, restarting in 5s", label)
                await asyncio.sleep(5)
                # Reload from DB in case tokens changed
                fresh = db.get_streamer_by_id(sid)
                if fresh:
                    session = StreamerSession(fresh)
                    self._sessions[sid] = session

    async def stop_all(self) -> None:
        self._stopping = True
        for t in self._tasks.values():
            t.cancel()
        for t in self._tasks.values():
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await t
        self._sessions.clear()
        self._tasks.clear()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    await no_cache.cache.start()
    supervisor = Supervisor()
    app.state.supervisor = supervisor
    await supervisor.spawn_all()
    log.info("nobot started: %d streamers installed, root_path=%r",
             len(supervisor._sessions), ROOT_PATH)
    try:
        yield
    finally:
        log.info("nobot shutting down")
        await supervisor.stop_all()
        await no_cache.cache.stop()


app = FastAPI(lifespan=lifespan, title="noBot for JoystickTV")
app.include_router(oauth.router)
app.include_router(panel.router)
app.mount(
    f"{ROOT_PATH}/static",
    StaticFiles(directory="static"),
    name="static",
)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=HOST,
        port=PORT,
        log_config=None,  # we've already set up logging
    )
