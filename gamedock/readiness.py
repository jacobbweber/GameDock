"""Is the game actually joinable? (pure functions, no I/O)

A container being "running" only means the program inside has started. A game server that first downloads
several GB through SteamCMD is not joinable for many minutes. GameDock combines three signals:

  1. the Docker health check of the image (``healthy`` / ``unhealthy`` / ``starting``), if the image has one;
  2. an optional catalog line ``ready_log_pattern``: a regular expression for the log line the game prints once
     it accepts players;
  3. an optional catalog line ``progress_log_pattern``: a regular expression with one capture group giving a
     percentage, for example SteamCMD's ``Update state (0x61) downloading, progress: 51.68``.

Result phases (only meaningful while the container runs):

  ready       the game can be joined (healthy, or the ready line was seen, or there is no signal at all)
  installing  downloading or installing game files (with ``progress`` when the log shows a percentage)
  starting    the container is up but the game is not ready yet
  unhealthy   Docker's health check keeps failing (Docker only says so after the image's start period and its retries;
              the service layer then looks for a known problem in the logs, see gamedock/problems.py)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

PHASE_READY, PHASE_INSTALLING, PHASE_STARTING, PHASE_UNHEALTHY = "ready", "installing", "starting", "unhealthy"
PHASE_FAILED = "failed"   # 0.6.3: the container is up but the game gave up (see problems.FATAL_IDS); set by the service

# CSI sequences (colours, cursor moves), OSC sequences (window titles) and lone ESC commands.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Progress only counts while it is near the end of the log: once the download is over, later lines push it out,
# so an old "99.9%" cannot keep a started server in the "installing" state.
PROGRESS_WINDOW_LINES = 60


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def clean_log_text(raw: str, collapse_cr: bool = True) -> str:
    """Make log text safe and readable: no ANSI colours, no control characters. Terminal progress bars rewrite
    one line with carriage returns; with collapse_cr only the last version of such a line is kept."""
    text = strip_ansi(raw).replace("\r\n", "\n")
    if collapse_cr:
        lines = []
        for line in text.split("\n"):
            if "\r" in line:
                parts = [p for p in line.split("\r") if p != ""]
                line = parts[-1] if parts else ""
            lines.append(line)
        text = "\n".join(lines)
    else:
        text = text.replace("\r", "\n")
    return _CONTROL_RE.sub("", text)


def pattern_seen(text: str, pattern: str) -> bool:
    """True if `pattern` (a regular expression) matches anywhere in the log text."""
    if not pattern:
        return False
    try:
        return re.search(pattern, strip_ansi(text)) is not None
    except re.error:
        return False


def last_progress(text: str, pattern: str) -> float | None:
    """The newest percentage in the last few log lines, or None. `pattern` has one capture group."""
    if not pattern:
        return None
    try:
        rx = re.compile(pattern)
    except re.error:
        return None
    segments = [s for s in re.split(r"[\r\n]+", strip_ansi(text)) if s.strip()]
    for seg in reversed(segments[-PROGRESS_WINDOW_LINES:]):
        m = rx.search(seg)
        if not m or not m.groups():
            continue
        try:
            value = float(m.group(1))
        except (TypeError, ValueError):
            continue
        if 0.0 <= value <= 100.0:
            return round(value, 2)
    return None


@dataclass(frozen=True)
class Readiness:
    phase: str
    progress: float | None = None
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.phase == PHASE_READY


def decide(health: str, has_ready_pattern: bool, ready_seen: bool, progress: float | None) -> Readiness:
    """Combine the signals (see the module text). `health` is Docker's health status or "" (no health check)."""
    has_signal = bool(health) or has_ready_pattern
    if health == "healthy" or ready_seen:
        return Readiness(PHASE_READY)
    if not has_signal:
        # nothing tells us the game is not ready, except an unfinished download we can see in the log
        if progress is not None and progress < 100:
            return _installing(progress)
        return Readiness(PHASE_READY)
    if progress is not None and progress < 100:
        return _installing(progress)
    if health == "unhealthy":
        return Readiness(PHASE_UNHEALTHY,
                         detail="Docker's health check for this game keeps failing: the container is running, but the game is "
                                "not answering the way it should. Open Logs to see what it printed, or Check setup for a "
                                "checklist with fixes.")
    return Readiness(PHASE_STARTING,
                     detail="The container is running but the game is not ready to join yet. "
                            "The first start can take several minutes.")


def _installing(progress: float) -> Readiness:
    return Readiness(PHASE_INSTALLING, progress,
                     f"Downloading and installing the game files ({progress:g}%). It cannot be joined until this "
                     "finishes; the first start can take several minutes.")
