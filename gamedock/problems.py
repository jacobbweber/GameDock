"""Known problems: turn a log line into a plain-language explanation and a next step (pure functions, no Docker).

A server that is running but not ready (or has stopped) is usually stuck for a reason the game already wrote down.
GameDock looks for that reason in the newest log lines of the container and, when the catalog entry says so, in the
game's own log files. Two lists of patterns are used, in this order:

  1. the catalog entry's ``fail_log_patterns`` (specific to the game: for example "Steam rejected the login token");
  2. the built-in patterns below (things any game can run into: out of memory, a port in use, a full disk, SteamCMD
     errors, a refused Steam login). They are deliberately conservative: each one needs wording that is rarely harmless.

The first pattern (in that order) that matches a line wins; within one pattern the NEWEST matching line is shown.
The line shown is cleaned and redacted (known secret values, ``password=...``, ``Bearer ...``) before it leaves here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from . import checks as ck
from .models import FailPattern
from .readiness import clean_log_text

MAX_LINE = 1000          # a log line longer than this is cut before matching (keeps matching fast and bounded)
MAX_EXCERPT = 240        # characters of the matching line that are shown
SOURCE_CATALOG, SOURCE_BUILTIN, SOURCE_DOCKER = "catalog", "builtin", "docker"


@dataclass(frozen=True)
class Problem:
    id: str
    title: str
    advice: str
    source: str            # catalog | builtin | docker
    where: str = ""        # "the container log" or the file path the line came from
    excerpt: str = ""      # the matching line, secrets hidden
    # 0.6.3: the game cannot start at all (refused its settings, or its process manager gave up). The card then says
    # "Failed to start" instead of "Starting". Taken from the log when it says so: the setting, its value, the allowed
    # values; config_path / setting_path are filled in by the service from the catalog's setting rules.
    fatal: bool = False
    setting: str = ""
    value: str = ""
    allowed: tuple[str, ...] = ()
    config_path: str = ""
    setting_path: str = ""

    def to_dict(self) -> dict:
        d = {"id": self.id, "title": self.title, "advice": self.advice, "source": self.source,
             "where": self.where, "excerpt": self.excerpt}
        if self.fatal:
            d["fatal"] = True
        for k in ("setting", "value", "config_path", "setting_path"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.allowed:
            d["allowed"] = list(self.allowed)
        return d

    @property
    def sentence(self) -> str:
        """Title and advice as one block of plain text (for the card, Check setup and the diagnostics text)."""
        return f"{self.title} {self.advice}".strip()


# id, regular expression (case-insensitive), title, advice
_BUILTIN: tuple[tuple[str, str, str, str], ...] = (
    # 0.6.3: a settings file the game refuses (first, so it wins over "the process gave up" that follows it in the log)
    ("config-invalid", r"\bFailed to parse (?:Config|enum|config|settings|the config)\b|\bSupported '?[\w.]+'? values\b|"
                       r"\bInternal Error: Failed to parse\b|\bInvalid value '[^'\n]{0,80}'[^\n]{0,40}\b(?:enum|setting|config)\b",
     "The game refused its settings file and cannot start.",
     "A setting has a value the game does not accept. Open Config, fix the value (the form shows the allowed ones), save "
     "and start the server again."),
    ("process-gave-up", r"\bgave up: [\w.:-]+ entered FATAL state\b|\bentered FATAL state, too many start retries\b",
     "The game failed to start: it stopped again right after each try, so its process manager gave up.",
     "The container is still up, but the game inside is not running. Look a few lines above in the Logs for the reason "
     "(often a settings file the game cannot read), fix it, then stop and start the server."),
    ("steam-state", r"Error! App '?\d+'? state is 0x(?:202|402|602)\b",
     "SteamCMD could not finish installing or updating the game files.",
     "This usually means the disk is full, the data folder cannot be written, or the download was interrupted. Check the "
     "free space under Health, then stop and start the server so SteamCMD tries again."),
    ("steam-no-subscription", r"\bNo subscription\b",
     "Steam says the account used for the download has no access to this game's server files (No subscription).",
     "Some dedicated servers can only be downloaded with a Steam account that owns the game. See the image's own "
     "documentation for its Steam login settings, then Edit the server and fill them in."),
    ("steam-missing-config", r"\bMissing configuration\b",
     "SteamCMD reported Missing configuration, so it could not read the game's download settings.",
     "This is often temporary (Steam was busy). Stop and start the server to try again. If it keeps happening, check that "
     "the machine can reach Steam and that the game's Steam App ID in the catalog entry is the dedicated server's."),
    ("steam-install-failed", r"\bFailed to install app\b|\bERROR! Failed to (?:install|update) app\b",
     "SteamCMD failed to install or update the game.",
     "Look a few lines above in the Logs for the reason (disk space, network, or a wrong App ID). Stop and start the "
     "server to try again once that is fixed."),
    ("steam-login", r"\b(?:Login Failure|Invalid Password|Rate Limit Exceeded|Two-?factor code|Steam Guard code)\b|"
                    r"\bFailed to log ?in\b[^\n]{0,40}\bSteam\b",
     "Steam refused the login used to download the game.",
     "If this game needs a Steam account, check the username, password or login token on the server's settings (Edit). "
     "Repeated tries can lock the login for a while: wait a few minutes before starting again."),
    ("steam-token-expired", r"\bk_EResultGSLTExpired\b",
     "Steam says the login token has expired.",
     "On steamcommunity.com/dev/managegameservers regenerate the token, or delete it and create a new one for the right App ID, then Edit the server and paste the new token. If new tokens also show as expired, the Steam account may be limited or lack a phone number."),
    ("steam-token", r"\bCustomAuthenticationFailed\b|\bk_EResult(?:AccountNotFound|InvalidPassword|AccessDenied|"
                    r"InvalidLoginAuthCode|Expired|Revoked)\b|\bFailed to connect to Steam\b[^\n]{0,60}\breason\b",
     "Steam refused the game server's login token.",
     "Create a new Game Server Login Token for this game (https://steamcommunity.com/dev/managegameservers), make sure "
     "it is the kind of key the game asks for and not a Steam Web API key, then Edit the server, paste the new token "
     "and start it again."),
    ("out-of-memory", r"\b(?:OutOfMemoryException|std::bad_alloc|Cannot allocate memory)\b|"
                      r"\bOut of memory:\s*Killed process\b|\bout of memory\b",
     "The game reports that it ran out of memory.",
     "Give the machine more free memory, stop other servers, or lower the player count. Check Monitoring for how much "
     "memory this server uses."),
    ("port-in-use", r"\baddress already in use\b|\bEADDRINUSE\b|\bbind(?:ing)? (?:to )?(?:port )?\d{2,5}\b[^\n]{0,40}"
                    r"\b(?:failed|error)\b|\bport \d{2,5} (?:is )?already in use\b",
     "The game says a network port is already in use.",
     "Another program (or another server) holds that port. Use Check setup to see the ports, stop the other program, or "
     "Edit this server and choose different host ports."),
    ("permission-denied", r"\b(?:mkdir|cannot create directory|cannot touch|cannot open|failed to open|unable to open|"
                          r"cannot write|failed to write|can't create)\b[^\n]{0,200}\bpermission denied\b|"
                          r"\bpermission denied\b[^\n]{0,80}(?:/home/|/data\b|/saves\b|/config\b|/server\b|/srv/)",
     "The game was not allowed to write to its data folder (permission denied).",
     "The folder under GameDock's data folder is probably owned by another user (often root) or read-only. Open Check "
     "setup and press Fix ownership (it gives the files to GameDock's user and deletes nothing); if the game runs as "
     "yet another user, map its PUID/PGID with user_env in the catalog entry and redeploy. Then start the server again."),
    ("disk-full", r"\bNo space left on device\b|\bENOSPC\b|\bdisk (?:is )?full\b|\bnot enough (?:free )?disk space\b",
     "The disk is full (no space left on device).",
     "Free some space on the machine that holds GameDock's data folder: delete old backups or unused servers, then "
     "start the server again. Health shows how much room is left."),
)
BUILTIN_PATTERNS: tuple[FailPattern, ...] = tuple(FailPattern("(?i)" + rx, title, advice) for _, rx, title, advice in _BUILTIN)
_BUILTIN_IDS = tuple(i for i, *_ in _BUILTIN)
BUILTIN_IDS = _BUILTIN_IDS
FATAL_IDS = frozenset({"config-invalid", "process-gave-up"})

# What a game says about a bad setting (Enshrouded: "Failed to parse enum 'TombstoneMode: Invalid value 'Off'." and
# "Supported 'TombstoneMode' values: AddBackpackMaterials, Everything, NoTombstone"). Kept loose on purpose.
_BAD_VALUE_RES = (
    re.compile(r"enum '(?P<setting>[\w.\[\]-]{1,80}):? *Invalid value '(?P<value>[^'\n]{0,80})'", re.I),
    re.compile(r"Invalid value '(?P<value>[^'\n]{0,80})' for (?:enum |setting |key |option )?'?(?P<setting>[\w.\[\]-]{1,80})'?", re.I),
    re.compile(r"'?(?P<setting>[\w.\[\]-]{1,80})'?:? *Invalid value '(?P<value>[^'\n]{0,80})'", re.I),
)
_SUPPORTED_RE = re.compile(r"Supported '?(?P<setting>[\w.\[\]-]{1,80})'? values:? *(?P<values>[^\n]{1,400})", re.I)


def config_error_detail(lines: Sequence[str]) -> tuple[str, str, tuple[str, ...]] | None:
    """(setting, value, allowed values) from the newest lines that name a refused setting, or None."""
    setting = value = ""
    allowed: tuple[str, ...] = ()
    for line in reversed(lines):
        if not allowed:
            m = _SUPPORTED_RE.search(line)
            if m and (not setting or m.group("setting").lower() == setting.lower()):
                vals = [v.strip(" .'\"[]") for v in re.split(r"[,|]", m.group("values"))]
                allowed = tuple(v for v in vals if v and len(v) <= 80)[:60]
                setting = setting or m.group("setting")
        if not value:
            for rx in _BAD_VALUE_RES:
                m = rx.search(line)
                if m:
                    if setting and m.group("setting").lower() != setting.lower():
                        continue
                    setting, value = m.group("setting"), m.group("value")
                    break
        if value and allowed:
            break
    if not setting:
        return None
    return setting, value, allowed


def _lines(text: str) -> list[str]:
    cleaned = clean_log_text(text or "")
    return [ln[:MAX_LINE] for ln in cleaned.split("\n") if ln.strip()]


def docker_problem(exit_code: int | None, oom_killed: bool) -> Problem | None:
    """A container that was killed for memory (Docker's OOMKilled flag, or exit code 137) is a problem on its own, whatever the
    log says. 137 = SIGKILL, which is what the kernel's out-of-memory killer sends; a manual `docker kill` looks the same."""
    if oom_killed:
        return Problem("out-of-memory", "The game was stopped because it ran out of memory.",
                       "Give the machine more free memory, stop other servers, or lower the player count, then start the "
                       "server again.", SOURCE_DOCKER, "Docker", "the container was killed by the out-of-memory killer")
    if exit_code == 137:
        return Problem("killed", "The game was killed (exit code 137).",
                       "That is what happens when the machine runs out of memory, or when something stops the container "
                       "by force. Check free memory under Monitoring, then start the server again.", SOURCE_DOCKER,
                       "Docker", "exit code 137")
    return None


def scan(sources: Sequence[tuple[str, str]], catalog_patterns: Iterable[FailPattern] = (), use_builtin: bool = True,
         secrets: Iterable[str] = ()) -> Problem | None:
    """The problem that best explains a stuck server, or None.

    `sources` is [(where, text)] (the container log first, then any extra log files). Catalog patterns are tried before
    the built-in ones; the first pattern that matches anything wins and the newest matching line (last source first) is shown.
    """
    prepared = [(where, _lines(text)) for where, text in sources if text]
    if not prepared:
        return None
    secret_values = tuple(s for s in secrets if s)
    entries: list[tuple[str, FailPattern, str]] = [(f"catalog-{i}", p, SOURCE_CATALOG)
                                                   for i, p in enumerate(catalog_patterns, 1)]
    if use_builtin:
        entries += [(pid, FailPattern(p.pattern, p.title, p.advice), SOURCE_BUILTIN)
                    for pid, p in zip(_BUILTIN_IDS, BUILTIN_PATTERNS)]
    for pid, fp, source in entries:
        try:
            rx = re.compile(fp.pattern)
        except re.error:
            continue
        for where, lines in reversed(prepared):
            for line in reversed(lines):
                if rx.search(line):
                    shown = ck.redact_text(line.strip(), secret_values)
                    if len(shown) > MAX_EXCERPT:
                        shown = shown[:MAX_EXCERPT - 1] + "\u2026"
                    p = Problem(pid, fp.title, fp.advice, source, where, shown,
                                fatal=pid in FATAL_IDS or (source == SOURCE_CATALOG and _looks_like_config_error(shown)))
                    return _with_config_detail(p, [ln for _, ls in prepared for ln in ls])
    return None


def _with_config_detail(p: Problem, lines: list[str]) -> Problem:
    """Name the refused setting, its value and the allowed values when the log says them."""
    from dataclasses import replace as _replace
    if p.id not in FATAL_IDS and not (p.source == SOURCE_CATALOG and _looks_like_config_error(p.excerpt)):
        return p
    got = config_error_detail(lines[-400:])
    if got is None:
        return p
    setting, value, allowed = got
    title = (f"The game refused its settings: {value!r} is not a valid value for {setting}." if value else
             f"The game refused its settings: the value of {setting} is not valid.")
    advice = ((f"Allowed values: {', '.join(allowed)}. " if allowed else "")
              + "Open Config, choose one of the allowed values for this setting, save, and start the server again.")
    if p.source == SOURCE_CATALOG:
        title, advice = p.title, (f"{p.advice} " if p.advice else "") + (f"Here: {setting} = {value!r}" if value else f"Setting: {setting}") \
            + (f"; allowed: {', '.join(allowed)}." if allowed else ".")
    return _replace(p, id="config-invalid" if p.source != SOURCE_CATALOG else p.id, title=title[:300], advice=advice[:900],
                    fatal=True, setting=setting, value=value, allowed=allowed)


def _looks_like_config_error(text: str) -> bool:
    return bool(re.search(r"Failed to parse|Invalid value|Supported .{0,80} values|entered FATAL state", text or "", re.I))


