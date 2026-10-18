"""Background sampler: a daemon thread that asks the service for a stats sample every N seconds."""
from __future__ import annotations

import logging
from threading import Event, Thread

from .service import GameDockService

log = logging.getLogger("gamedock.sampler")


class Sampler:
    def __init__(self, service: GameDockService, interval: float | None = None) -> None:
        self.service = service
        self.interval = interval or service.stats_interval
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="gamedock-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        delay = 0.0
        while not self._stop.wait(delay):
            try:
                snap = self.service.sample_stats()
                # idle (nothing running): back off to half the rate; running: normal rate
                delay = self.interval if any(s.running for s in snap.servers) else self.interval * 2
            except Exception:  # never let monitoring kill the thread
                log.exception("stats sample failed")
                delay = self.interval * 2
