"""The in-process scheduler: one small background thread, no cron and no extra program.

Every TICK seconds it asks the service to start whatever is due (backups, maintenance), asks GitHub whether a newer
GameDock exists (at most every 6 hours) and asks the image registry about one server whose answer is old (answers
are kept for hours). Jobs run in their own threads, so a long image download never delays the next tick.
"""
from __future__ import annotations

import logging
from threading import Event, Thread

from .service import GameDockService

log = logging.getLogger("gamedock.scheduler")
TICK_SECONDS = 30.0
FIRST_DELAY = 5.0   # let the web server come up first; runs missed while GameDock was down start right after


class Scheduler:
    def __init__(self, service: GameDockService, tick: float = TICK_SECONDS, first_delay: float = FIRST_DELAY,
                 background_checks: bool = True) -> None:
        self.service = service
        self.tick = tick
        self.first_delay = first_delay
        self.background_checks = background_checks   # False when GAMEDOCK_UPDATE_CHECK=false
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="gamedock-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def run_once(self) -> None:
        """One tick (also used by tests)."""
        try:
            self.service.process_schedules()
        except Exception:  # noqa: BLE001 - the loop must survive anything
            log.exception("scheduler tick failed")
        if not self.background_checks or self.service._frozen:
            return
        try:
            self.service.updater.check()
        except Exception:  # noqa: BLE001
            log.exception("GameDock update check failed")
        try:
            self.service.check_image_updates_due(limit=1)
        except Exception:  # noqa: BLE001
            log.exception("image update check failed")

    def _run(self) -> None:
        delay = self.first_delay
        while not self._stop.wait(delay):
            self.run_once()
            delay = self.tick
