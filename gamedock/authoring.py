"""Adding games to the catalog: the field reference, strict checking of a catalog file given as text, a review step
for entries an AI agent adds, and the files that go with it.

Two kinds of catalog entry exist:
  * repo entries   - files a person put in the catalog folder (usually through git). Trusted; never touched here.
  * AI-added ones  - written by `add_catalog_entry` over MCP. They get the strict rules (see catalog.safety_problems),
                     are recorded in <data>/catalog-review.json as NOT reviewed, and cannot be deployed until a person
                     approves exactly that file in the web page (Approve records the file's SHA-256; any later change
                     to the file, by anyone, makes it unreviewed again).

Nothing in this module runs or pulls a Docker image. The web page and the MCP tools both call GameDockService, which
calls CatalogAuthoring.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

import yaml

from .catalog import ENTRY_KEYS, MAX_PATTERN_LEN, parse_definition, safety_problems
from .errors import CatalogError, ConflictError, NotFoundError, OperationError, ValidationError
from .models import ID_RE, CatalogEntry

REVIEW_FILENAME = "catalog-review.json"
MAX_YAML_BYTES = 20 * 1024
MIN_AI_HOST_PORT = 1024
LIMITS = {
    "max_yaml_bytes": MAX_YAML_BYTES,
    "max_pattern_length": MAX_PATTERN_LEN,
    "min_host_port_for_ai_added_entries": MIN_AI_HOST_PORT,
    "host_folders_for_ai_added_entries": "relative only (kept inside the server's own data folder)",
}

# --------------------------------------------------------------------------- the field reference
# One row per catalog key. docs/ADDING_A_GAME.md contains this table (generated: run scripts/update_docs.py) and
# get_add_game_guide returns it. tests/test_authoring.py checks that it names exactly the keys the loader knows.
_ID_RULE = f"lowercase letters, digits, - and _, starting with a letter or digit, at most 63 characters"
FIELD_REFERENCE: list[dict] = [
    {"key": "id", "type": "text", "required": True, "default": "(none)",
     "meaning": f"The short name of the game. {_ID_RULE.capitalize()}. Becomes the file name <id>.yaml. Ids that start "
                "with _ are reserved for templates."},
    {"key": "name", "type": "text", "required": True, "default": "(none)",
     "meaning": "The name people see on the card, for example 'Example Game Dedicated Server'."},
    {"key": "image", "type": "text", "required": True, "default": "(none)",
     "meaning": "The Docker image, as name[:tag][@digest], for example 'someuser/some-server:1.2'. Letters, digits and "
                ". _ - / : @ only. Prefer a pinned tag over 'latest'."},
    {"key": "description", "type": "text", "required": False, "default": "empty",
     "meaning": "One line shown on the card. At most 2000 characters."},
    {"key": "notes", "type": "text", "required": False, "default": "empty",
     "meaning": "Shown in amber on the card. Use it for warnings, where the information came from, and what has NOT "
                "been verified yet."},
    {"key": "ports", "type": "list of ports", "required": False, "default": "none",
     "meaning": "The default ports the game needs. Each server can choose different host ports when deployed."},
    {"key": "ports[].protocol", "type": "tcp or udp", "required": True, "default": "(none)",
     "meaning": "The protocol. Many games use UDP for play and TCP for queries or remote control: list each one."},
    {"key": "ports[].host", "type": "whole number 1-65535", "required": True, "default": "(none)",
     "meaning": "The default port on this machine. Entries added by an AI agent must use 1024 or higher. The same "
                "host port and protocol may not appear twice."},
    {"key": "ports[].container", "type": "whole number 1-65535", "required": True, "default": "(none)",
     "meaning": "The port inside the container (what the image's documentation lists). The same container port and "
                "protocol may not appear twice."},
    {"key": "volumes", "type": "list of folders", "required": False, "default": "none",
     "meaning": "Folders from this machine that appear inside the container. Anything not in a volume is lost when the "
                "container is removed and is not backed up. Mount the saves, the settings and (for games that download "
                "many GB) the game's install folder."},
    {"key": "volumes[].host", "type": "relative folder", "required": True, "default": "(none)",
     "meaning": "A relative folder such as ./saves, kept under <data folder>/<server id>/. No '..'. Absolute paths are "
                "only accepted in entries a person put in the catalog folder; never the Docker socket."},
    {"key": "volumes[].container", "type": "absolute path", "required": True, "default": "(none)",
     "meaning": "Where the folder appears inside the container. Must start with / and contain no '..'."},
    {"key": "env", "type": "mapping name: value", "required": False, "default": "none",
     "meaning": "Fixed environment variables that are always set and are not shown in the deploy form (quote numbers "
                "and yes/no so YAML keeps them as text). Never put passwords here: the file is public. A name may not "
                "also be listed in fields."},
    {"key": "fields", "type": "list of settings", "required": False, "default": "none",
     "meaning": "Environment variables the person deploying fills in (server name, slots, password...)."},
    {"key": "fields[].name", "type": "environment variable name", "required": True, "default": "(none)",
     "meaning": "Letters, digits and _, not starting with a digit. Names such as PATH, HOME or DOCKER_* are reserved. "
                "Each name once."},
    {"key": "fields[].label", "type": "text", "required": False, "default": "the name",
     "meaning": "The label in the form."},
    {"key": "fields[].default", "type": "text", "required": False, "default": "empty",
     "meaning": "The value pre-filled in the form. An empty value is still passed to the container as NAME= (present "
                "but empty), because some images treat that differently from a missing variable."},
    {"key": "fields[].secret", "type": "true or false", "required": False, "default": "false",
     "meaning": "true = a password box whose value is never shown again, never written to logs and never returned by "
                "the API. Use it for every password."},
    {"key": "fields[].required", "type": "true or false", "required": False, "default": "false",
     "meaning": "true = the person must fill it in when deploying."},
    {"key": "fields[].description", "type": "text", "required": False, "default": "empty",
     "meaning": "Help text under the box. Say what an empty value means (for example 'leave empty for no password')."},
    {"key": "config_files", "type": "list of files", "required": False, "default": "none",
     "meaning": "Settings files people may read and edit with the Config button. Each must be inside a mounted folder."},
    {"key": "config_files[].path", "type": "relative file path", "required": True, "default": "(none)",
     "meaning": "Relative to the server's data folder, so it starts with the volume's host folder, for example "
                "saves/settings.ini. No '..', no absolute path. Each path once."},
    {"key": "config_files[].label", "type": "text", "required": False, "default": "the path",
     "meaning": "The name shown above the editor."},
    {"key": "config_files[].format", "type": "ini, json, yaml or text", "required": False, "default": "text",
     "meaning": "A hint for the editor. json files are checked for validity when saved. Files shaped like Palworld's "
                "OptionSettings, KEY=value lines, or JSON also get a form with one box per setting."},
    {"key": "config_files[].sensitive_keys", "type": "list of names", "required": False, "default": "none",
     "meaning": "Settings whose values are hidden in the editor and in the form (passwords). Writing the placeholder "
                "back keeps the stored value. Names: letters, digits, _ . -"},
    {"key": "config_files[].hot_edit", "type": "true or false", "required": False, "default": "false",
     "meaning": "true = safe to edit while the server runs. false = GameDock asks for the server to be stopped first "
                "(the game would overwrite the file when it exits)."},
    {"key": "config_files[].note", "type": "text", "required": False, "default": "empty",
     "meaning": "A warning shown above the editor, for example 'the image rewrites this file on every start'."},
    {"key": "update_method", "type": "pull_recreate or none", "required": False, "default": "pull_recreate",
     "meaning": "pull_recreate = the Update button downloads the newest image and recreates the container. none = the "
                "game updates itself inside the container and image updates are switched off."},
    {"key": "update_note", "type": "text", "required": False, "default": "empty",
     "meaning": "Plain words shown with the update button: how this particular image really updates the game."},
    {"key": "backup_stop_first", "type": "true or false", "required": False, "default": "false",
     "meaning": "true = backups stop the game briefly instead of copying it while it runs. Use it for games that keep "
                "their save in memory or write it in pieces."},
    {"key": "save_command", "type": "list of words (or one quoted string)", "required": False, "default": "none",
     "meaning": "A command, run inside the container without a shell, that makes the game write its save, run before a "
                "backup of a running server. 1 to 12 words, each at most 300 characters. Example: [rcon-cli, save]. A "
                "failure only adds a note to the backup."},
    {"key": "ready_log_pattern", "type": "regular expression", "required": False, "default": "empty",
     "meaning": "A line the game prints once players can join. Until it appears the tile says Starting or Installing, "
                f"not Ready to join. One line, at most {MAX_PATTERN_LEN} characters, checked when the catalog loads."},
    {"key": "progress_log_pattern", "type": "regular expression with exactly one group", "required": False,
     "default": "empty",
     "meaning": "Matches a download progress line; the one group in brackets is the percentage, for example "
                "'progress: *([0-9.]+)'. Gives the tile its Installing 52% bar."},
    {"key": "steam_app_id", "type": "number (as text)", "required": False, "default": "empty",
     "meaning": "The Steam app id of the DEDICATED SERVER (not the game you play). Turns on game version tracking: "
                "installed build, newest build, Update available."},
    {"key": "steam_branch", "type": "text", "required": False, "default": "empty (the public branch)",
     "meaning": "The Steam branch to compare with, for example beta. Needs steam_app_id."},
    {"key": "steam_manifest_dir", "type": "relative folder", "required": False, "default": "empty (search the volumes)",
     "meaning": "The folder, inside the server's data folder, holding appmanifest_<app id>.acf, when the automatic "
                "search does not find it. Needs steam_app_id."},
    {"key": "version_log_pattern", "type": "regular expression with exactly one group", "required": False,
     "default": "empty",
     "meaning": "A log line that shows the game's own version, with the version in the one group, for example "
                "'Game version is (v?[0-9.]+)'. The last match is used."},
    {"key": "game_update_method", "type": "restart or none", "required": False,
     "default": "restart when steam_app_id is set, otherwise none",
     "meaning": "restart = the image updates the game when the container starts, so Update game now restarts the server "
                "and checks the build changed. none = GameDock cannot update the game itself. restart needs "
                "steam_app_id."},
    {"key": "installed_build_file", "type": "relative file path", "required": False, "default": "empty",
     "meaning": "A small file, inside the server's data folder, whose text is the installed version or build, for "
                "images that keep no Steam appmanifest. A plain number is compared with Steam's newest build."},
    {"key": "stop_timeout", "type": "whole number of seconds, 1-600", "required": False,
     "default": "0 (GameDock's usual stop time, 60 seconds)",
     "meaning": "How long this game gets to save and exit when stopped, before Docker kills it. Give slow-saving games "
                "more time."},
]


def field_reference() -> list[dict]:
    return [dict(r) for r in FIELD_REFERENCE]


def field_reference_markdown() -> str:
    """The table that docs/ADDING_A_GAME.md shows (kept equal to the doc by a test)."""
    lines = ["| Key | Type | Required | Default | Meaning |", "|---|---|---|---|---|"]
    for r in FIELD_REFERENCE:
        key = f"`{r['key']}`"
        lines.append(f"| {key} | {r['type']} | {'yes' if r['required'] else 'no'} | {r['default']} | "
                     f"{r['meaning'].replace('|', '/')} |")
    return "\n".join(lines)


def reference_keys() -> dict[str, set[str]]:
    """The keys named by FIELD_REFERENCE, grouped like the loader's schema: {"": top level, "ports": {...}, ...}."""
    out: dict[str, set[str]] = {"": set()}
    for r in FIELD_REFERENCE:
        key = r["key"]
        if "[]." in key:
            group, sub = key.split("[].", 1)
            out.setdefault(group, set()).add(sub)
        else:
            out[""].add(key)
    return out


# --------------------------------------------------------------------------- reading the text strictly
class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a key written twice in the same mapping (YAML would silently keep the last one)."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"the key {key!r} is written twice", key_node.start_mark)
            seen.add(key)
        except TypeError:
            pass
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _yaml_problem(e: yaml.YAMLError) -> str:
    mark = getattr(e, "problem_mark", None)
    where = f" (line {mark.line + 1})" if mark is not None else ""
    problem = getattr(e, "problem", None) or str(e).splitlines()[0]
    return f"The text is not valid YAML{where}: {problem}."


@dataclass
class Checked:
    errors: list[str]
    data: dict | None = None
    entry: CatalogEntry | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors and self.entry is not None


def check_text(yaml_text: object) -> Checked:
    """Check one catalog entry given as text with the strict rules for AI-added entries. Never raises; never writes."""
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        return Checked(["The entry is empty. Send the complete YAML text of the catalog entry."])
    text = yaml_text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text.encode("utf-8", "replace")) > MAX_YAML_BYTES:
        return Checked([f"The entry is larger than {MAX_YAML_BYTES // 1024} KB. A catalog entry should be short: "
                        "shorten the comments and notes."], text=text)
    if "\0" in text:
        return Checked(["The text contains a NUL character, which is not allowed."], text=text)
    try:
        for ev in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(ev, yaml.AliasEvent):
                return Checked(["YAML anchors and aliases (&name and *name) are not allowed in catalog entries. "
                                "Write the value out in full."], text=text)
        data = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as e:
        return Checked([_yaml_problem(e)], text=text)
    if not isinstance(data, dict):
        return Checked(["The top level of the entry must be a list of 'key: value' lines (a mapping)."], text=text)
    errors = safety_problems(data, "mcp")
    entry: CatalogEntry | None = None
    gid = data.get("id")
    if isinstance(gid, str) and gid.startswith("_"):
        errors.append(f"The id {gid!r} starts with an underscore. Ids starting with _ are reserved for templates "
                      "and are ignored by the catalog.")
    try:
        entry = parse_definition({k: v for k, v in data.items() if k in ENTRY_KEYS}, "the entry",
                                 origin="repo" if errors else "mcp")
    except CatalogError as e:
        msg = str(e)
        msg = msg[len("the entry: "):] if msg.startswith("the entry: ") else msg
        if not (errors and "docker.sock" in msg and any("docker.sock" in x for x in errors)):
            errors.append(msg)
    except (TypeError, ValueError, AttributeError, KeyError) as e:  # a wrong shape the loader did not foresee
        errors.append(f"The entry has a part with the wrong shape ({type(e).__name__}). Compare it with the "
                      "template and the field reference.")
    if entry is not None and not errors:
        dup = _duplicate_host_ports(entry)
        if dup:
            errors.append(dup)
    return Checked(errors, data, entry if not errors else None, text)


def _duplicate_host_ports(entry: CatalogEntry) -> str:
    seen = set()
    for p in entry.ports:
        if (p.protocol, p.host) in seen:
            return f"The host port {p.host}/{p.protocol} is listed twice. Each host port and protocol may appear once."
        seen.add((p.protocol, p.host))
    return ""


# --------------------------------------------------------------------------- plain-language summary and flags
@dataclass(frozen=True)
class Flag:
    level: str   # "warn" | "info"
    text: str


SECRET_NAME_RE = re.compile(r"pass|secret|token|key|pwd|credential", re.I)
RISKY_CONTAINER_PATHS = {"/", "/etc", "/proc", "/sys", "/dev", "/bin", "/sbin", "/usr", "/lib", "/lib64", "/boot",
                         "/run", "/var/run"}


def _image_parts(image: str) -> tuple[str, str, str]:
    """(registry host or '', tag or '', digest or '')"""
    ref, _, digest = image.partition("@")
    first, _, rest = ref.partition("/")
    registry = first if rest and ("." in first or ":" in first or first == "localhost") else ""
    last = ref.rsplit("/", 1)[-1]
    tag = last.split(":", 1)[1] if ":" in last else ""
    return registry, tag, digest


def analyze_entry(e: CatalogEntry, others: Mapping[str, CatalogEntry] | None = None) -> list[Flag]:
    """Things a person reviewing the entry (or the agent writing it) should look at, in plain words."""
    flags: list[Flag] = []
    add = lambda level, text: flags.append(Flag(level, text))  # noqa: E731
    registry, tag, digest = _image_parts(e.image)
    if registry:
        add("warn", f"The image comes from {registry}, not from Docker Hub. Make sure you trust that registry.")
    if not digest and tag in ("", "latest"):
        add("warn", "The image has no fixed version (no tag, or the tag 'latest'), so the game can change under you "
                    "when the image is updated. Use a pinned tag if the image publishes one.")
    if not e.ports:
        add("warn", "The entry opens no ports, so nobody can join the game.")
    if not e.volumes:
        add("warn", "The entry mounts no folders. Nothing the game saves would survive removing the container, and "
                    "GameDock could not back anything up.")
    for v in e.volumes:
        if v.host.startswith(("/", "~")):
            add("warn", f"A folder from outside GameDock's data folder is mounted: {v.host} -> {v.container}.")
        if v.container.rstrip("/") in RISKY_CONTAINER_PATHS or v.container == "/":
            add("warn", f"A folder is mounted over {v.container} inside the container, which is a system folder.")
    for p in e.ports:
        if p.host < 1024:
            add("warn", f"Host port {p.host}/{p.protocol} is below 1024 (a privileged port).")
    if len(e.ports) > 12:
        add("warn", f"The entry opens {len(e.ports)} ports, which is a lot for one game.")
    for k, v in e.env.items():
        if SECRET_NAME_RE.search(k) and v:
            add("warn", f"The fixed setting {k} looks like a password but has a value written in the file. Catalog "
                        "files are not secret: make it a field with secret: true instead.")
    for f in e.fields:
        if f.secret and f.default:
            add("warn", f"The secret field {f.name} has a default value written in the file.")
        if not f.secret and SECRET_NAME_RE.search(f.name) and "PASSWORD" in f.name.upper():
            add("warn", f"The field {f.name} looks like a password but is not marked secret: true, so it would be "
                        "shown in the form and returned by the API.")
    if not e.description.strip():
        add("warn", "There is no description for the card.")
    if not e.notes.strip():
        add("warn", "There are no notes. Use notes to say where the image information came from and what has not "
                    "been verified yet.")
    if not e.ready_log_pattern:
        add("warn" if e.steam_app_id else "info",
            "No ready_log_pattern: the tile will say Ready to join as soon as the container runs, which is too early "
            "for games that download or install on the first start.")
    if e.steam_app_id and not e.progress_log_pattern:
        add("info", "No progress_log_pattern: the first-start download shows no percentage.")
    if not e.steam_app_id:
        add("info", "No steam_app_id: GameDock will not show the game's own version or Update available.")
    if e.volumes and not e.config_files:
        add("info", "No config_files: people cannot edit the game's settings file from GameDock.")
    if e.save_command:
        add("info", "Before a backup of a running server GameDock runs this command inside the container: "
                    + " ".join(e.save_command))
    elif not e.backup_stop_first:
        add("info", "Backups of a running server copy the files while the game runs (no save_command and "
                    "backup_stop_first is false). Fine for games that save to disk continuously.")
    if others:
        for p in e.ports:
            for o in others.values():
                if o.id != e.id and any(q.host == p.host and q.protocol == p.protocol for q in o.ports):
                    add("info", f"The default host port {p.host}/{p.protocol} is also the default of '{o.id}'. They "
                                "can still both be deployed if one of them is given another host port.")
                    break
    return flags


def summarize_entry(e: CatalogEntry) -> dict:
    """What an entry will do, for the Review dialog and for validate results."""
    ports = [{"protocol": p.protocol, "host": p.host, "container": p.container} for p in e.ports]
    folders = [{"host": v.host, "container": v.container} for v in e.volumes]
    secrets = [f.name for f in e.fields if f.secret]
    lines = [f"Runs the Docker image {e.image}."]
    if ports:
        lines.append("Opens " + ", ".join(f"{p['host']} ({p['protocol'].upper()}, inside the container {p['container']})"
                                          for p in ports) + " on this machine.")
    else:
        lines.append("Opens no ports.")
    if folders:
        lines.append("Mounts " + ", ".join(f"{v['host']} as {v['container']}" for v in folders)
                     + (" (each relative folder is kept inside that server's own data folder)."
                        if all(not v["host"].startswith(("/", "~")) for v in folders) else "."))
    else:
        lines.append("Mounts no folders.")
    if e.fields:
        lines.append("Asks whoever deploys it for: " + ", ".join(f.name for f in e.fields) + ".")
    if secrets:
        lines.append("Secret (password) boxes: " + ", ".join(secrets) + ".")
    if e.env:
        lines.append(f"Sets {len(e.env)} fixed environment value(s) without asking: " + ", ".join(e.env) + ".")
    if e.config_files:
        lines.append("Lets people edit: " + ", ".join(c.path for c in e.config_files) + ".")
    if e.steam_app_id:
        lines.append(f"Tracks game versions with Steam app id {e.steam_app_id}.")
    return {"id": e.id, "name": e.name, "image": e.image, "ports": ports, "folders": folders,
            "fields": [f.name for f in e.fields], "secret_fields": secrets, "fixed_env_names": list(e.env),
            "config_files": [c.path for c in e.config_files], "steam_app_id": e.steam_app_id, "lines": lines}


# --------------------------------------------------------------------------- the review record
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class ReviewStore:
    """<data>/catalog-review.json: {entry id: {added_by, added_at, reviewed, yaml_sha256, file, ...}}.
    Only entries added by an AI agent are listed. Never part of the repo (the data folder is git-ignored)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.problem = ""

    def load(self) -> dict[str, dict]:
        with self._lock:
            self.problem = ""
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return {}
            except (OSError, UnicodeDecodeError) as e:
                self.problem = f"{REVIEW_FILENAME} cannot be read: {e}"
                return {}
            try:
                data = json.loads(raw)
            except ValueError as e:
                self.problem = f"{REVIEW_FILENAME} is not valid JSON ({e}); AI-added entries are not recognised until it is repaired."
                return {}
            if not isinstance(data, dict):
                self.problem = f"{REVIEW_FILENAME} has the wrong shape."
                return {}
            return {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def save(self, data: dict[str, dict]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp, self.path)

    def update(self, fn: Callable[[dict[str, dict]], None]) -> None:
        with self._lock:
            data = self.load()
            if self.problem:
                raise OperationError(self.problem + " Fix or remove that file first.")
            fn(data)
            self.save(data)


def _git_tracked(path: Path) -> bool:
    """True if `path` is a file git tracks (a file that came with the repository)."""
    try:
        r = subprocess.run(["git", "-C", str(path.parent), "ls-files", "--error-unmatch", "--", path.name],
                           capture_output=True, timeout=5, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class CatalogAuthoring:
    def __init__(self, catalog_dir: Path, data_dir: Path, entries: Callable[[], Mapping[str, CatalogEntry]],
                 changed: Callable[[], None] = lambda: None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.catalog_dir = Path(catalog_dir)
        self.store = ReviewStore(Path(data_dir) / REVIEW_FILENAME)
        self._entries = entries
        self._changed = changed
        self._clock = clock
        self._lock = threading.RLock()

    # ---- helpers
    def records(self) -> dict[str, dict]:
        return self.store.load()

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds").replace("+00:00", "Z")

    def _file_for(self, gid: str) -> Path:
        """The only place an entry's file may live: <catalog dir>/<id>.yaml (id already matches ID_RE)."""
        if not ID_RE.match(gid):
            raise ValidationError(f"'{gid}' is not a valid game id.")
        base = self.catalog_dir.resolve()
        path = (base / f"{gid}.yaml").resolve()
        if path.parent != base:
            raise ValidationError("The file would be outside the catalog folder, so it was refused.")
        return path

    def _existing_files(self, gid: str) -> list[Path]:
        base = self.catalog_dir
        return [p for ext in (".yaml", ".yml", ".json") for p in [base / f"{gid}{ext}"] if p.exists() or p.is_symlink()]

    def _write_atomic(self, path: Path, text: str, *, must_not_exist: bool) -> str:
        data = text.encode("utf-8")
        tmp = path.with_name("." + path.name + ".tmp")
        self.catalog_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if must_not_exist:
                if path.exists() or path.is_symlink():
                    raise ConflictError(f"A catalog file named {path.name} already exists, so nothing was written.")
                os.link(tmp, path) if hasattr(os, "link") else os.replace(tmp, path)
            else:
                os.replace(tmp, path)
        except FileExistsError:
            raise ConflictError(f"A catalog file named {path.name} already exists, so nothing was written.") from None
        except OSError as e:
            raise OperationError(f"The catalog file could not be written ({e.strerror or e}).") from e
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        return sha256_bytes(data)

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"
        return text

    def _result(self, c: Checked) -> dict:
        entries = self._entries()
        flags = analyze_entry(c.entry, entries) if c.entry else []
        gid = (c.data or {}).get("id") if isinstance(c.data, dict) else None
        info: dict = {
            "valid": c.ok, "errors": list(c.errors),
            "warnings": [f.text for f in flags if f.level == "warn"],
            "notes": [f.text for f in flags if f.level == "info"],
            "id": c.entry.id if c.entry else (gid if isinstance(gid, str) else None),
            "file_name": f"{c.entry.id}.yaml" if c.entry else None,
            "summary": summarize_entry(c.entry) if c.entry else None,
            "size_bytes": len(c.text.encode("utf-8", "replace")),
        }
        info.update(self._standing(c.entry.id) if c.entry else {"exists": False, "can_add": False, "can_update": False})
        return info

    def _standing(self, gid: str) -> dict:
        rec = self.records().get(gid)
        exists = gid in self._entries() or bool(self._existing_files(gid))
        out = {"exists": exists, "origin": "mcp" if rec else ("repo" if exists else None),
               "can_add": not exists, "can_update": False, "next_step": ""}
        if not exists:
            out["next_step"] = "Call add_catalog_entry with this text to save it. A person must then approve it in the web page."
        elif rec:
            tracked = _git_tracked(self._file_for(gid)) if self._existing_files(gid) else False
            out["can_update"] = not tracked
            out["next_step"] = ("An entry with this id was added through MCP: use update_catalog_entry to replace it "
                                "(it will need a new review)." if not tracked else
                                "That file is tracked by git, so MCP will not change it.")
        else:
            out["next_step"] = ("An entry with this id already exists and came from the repository. MCP never changes "
                                "those: choose a different id.")
        return out

    # ---- MCP-facing actions
    def validate(self, yaml_text: object) -> dict:
        return self._result(check_text(yaml_text))

    def add(self, yaml_text: object) -> dict:
        c = check_text(yaml_text)
        if not c.ok:
            raise ValidationError(_problems_message("The entry was NOT saved.", c.errors))
        gid = c.entry.id
        with self._lock:
            entries = self._entries()
            if gid in entries or self._existing_files(gid):
                raise ConflictError(
                    f"A catalog entry with the id '{gid}' already exists, so nothing was saved. MCP never overwrites "
                    "an existing entry. To change an entry that was added through MCP, use update_catalog_entry; to "
                    "add a different game, choose another id.")
            path = self._file_for(gid)
            text = self._normalize_text(c.text)
            digest = sha256_bytes(text.encode("utf-8"))
            now = self._now()
            rec = {"added_by": "mcp", "added_at": now, "reviewed": False, "yaml_sha256": digest, "file": path.name}
            # The record goes first: a file without a record would look like a trusted repository entry.
            self.store.update(lambda d: d.__setitem__(gid, rec))
            try:
                self._write_atomic(path, text, must_not_exist=True)
            except Exception:
                self.store.update(lambda d: d.pop(gid, None))
                raise
        self._changed()
        res = self._result(check_text(text))
        res.update({"status": "added", "id": gid, "file": path.name, "reviewed": False, "yaml_sha256": digest,
                    "can_deploy": False,
                    "next_step": "The entry is saved but marked 'Added by AI, needs your review'. It cannot be deployed "
                                 "until a person opens the Catalog page, reads the Review dialog and presses Approve. "
                                 "Tell the person that it is waiting."})
        res["exists"], res["can_add"] = True, False
        res["can_update"] = True
        return res

    def update(self, yaml_text: object) -> dict:
        c = check_text(yaml_text)
        if not c.ok:
            raise ValidationError(_problems_message("The entry was NOT changed.", c.errors))
        gid = c.entry.id
        with self._lock:
            rec = self.records().get(gid)
            if rec is None:
                if gid in self._entries() or self._existing_files(gid):
                    raise ConflictError(
                        f"The entry '{gid}' came from the repository, not from MCP. MCP never changes entries from "
                        "the repository. Choose another id, or ask the person to edit the file.")
                raise NotFoundError(f"There is no entry '{gid}' that was added through MCP. Use add_catalog_entry.")
            path = self._file_for(gid)
            if not path.exists():
                raise NotFoundError(f"The file of entry '{gid}' is missing. Use add_catalog_entry to add it again.")
            if _git_tracked(path):
                raise ConflictError(f"The file {path.name} is tracked by git, so MCP will not change it.")
            text = self._normalize_text(c.text)
            digest = sha256_bytes(text.encode("utf-8"))
            now = self._now()

            def fn(d: dict) -> None:
                r = dict(d.get(gid) or rec)
                r.update({"reviewed": False, "yaml_sha256": digest, "updated_at": now, "file": path.name})
                r.pop("reviewed_sha256", None)
                r.pop("reviewed_at", None)
                d[gid] = r

            self.store.update(fn)   # first: if writing the file then fails, the old file is simply unreviewed
            self._write_atomic(path, text, must_not_exist=False)
        self._changed()
        res = self._result(check_text(text))
        res.update({"status": "updated", "id": gid, "file": path.name, "reviewed": False, "yaml_sha256": digest,
                    "can_deploy": False,
                    "next_step": "The entry was replaced and needs a new review before it can be deployed. Servers "
                                 "already deployed from it keep the old settings."})
        res["exists"], res["can_add"], res["can_update"] = True, False, True
        return res

    # ---- web-facing actions
    def review_info(self, gid: str) -> dict:
        entry = self._entries().get(gid)
        if entry is None:
            raise NotFoundError(f"Unknown catalog entry '{gid}'")
        rec = self.records().get(gid)
        try:
            text = (self.catalog_dir / entry.source_file).read_text(encoding="utf-8")
        except OSError as e:
            raise OperationError(f"The file of '{gid}' cannot be read: {e.strerror or e}") from e
        flags = analyze_entry(entry, self._entries())
        changed_after_review = bool(rec and rec.get("reviewed") and not entry.reviewed)
        return {
            "id": gid, "name": entry.name, "origin": entry.origin, "reviewed": entry.reviewed,
            "added_by": (rec or {}).get("added_by"), "added_at": (rec or {}).get("added_at"),
            "reviewed_at": (rec or {}).get("reviewed_at") if entry.reviewed else None,
            "changed_after_review": changed_after_review,
            "file": entry.source_file, "yaml": text, "yaml_sha256": entry.file_sha256,
            "summary": summarize_entry(entry),
            "flags": [{"level": f.level, "text": f.text} for f in flags],
            "can_approve": bool(rec) and not entry.reviewed, "can_reject": bool(rec),
        }

    def approve(self, gid: str, yaml_sha256: str) -> dict:
        with self._lock:
            entry = self._entries().get(gid)
            rec = self.records().get(gid)
            if entry is None or rec is None:
                raise NotFoundError(f"'{gid}' is not an entry that was added by an AI agent, so there is nothing to approve.")
            if entry.reviewed:
                return {"id": gid, "reviewed": True, "note": "This entry was already approved."}
            if not yaml_sha256 or yaml_sha256 != entry.file_sha256:
                raise ConflictError("The entry changed while you were looking at it. Open the review again and read "
                                    "the new version before approving.")
            now = self._now()

            def fn(d: dict) -> None:
                r = dict(d[gid])
                r.update({"reviewed": True, "reviewed_sha256": entry.file_sha256, "reviewed_at": now,
                          "reviewed_by": "web"})
                d[gid] = r

            self.store.update(fn)
        self._changed()
        return {"id": gid, "reviewed": True, "reviewed_at": now,
                "note": f"Approved. '{entry.name}' can now be deployed."}

    def reject(self, gid: str, confirm: bool) -> dict:
        if not confirm:
            raise ValidationError("Rejecting deletes the entry's file for good. Confirm to go ahead.")
        with self._lock:
            rec = self.records().get(gid)
            if rec is None:
                raise NotFoundError(f"'{gid}' is not an entry that was added by an AI agent, so it cannot be rejected here.")
            path = self._file_for(gid)
            if path.exists():
                if _git_tracked(path):
                    raise ConflictError(f"The file {path.name} is tracked by git. Remove it with git instead.")
                try:
                    path.unlink()
                except OSError as e:
                    raise OperationError(f"The file could not be deleted ({e.strerror or e}).") from e
            self.store.update(lambda d: d.pop(gid, None))
        self._changed()
        return {"id": gid, "deleted": True, "note": f"The entry '{gid}' was deleted."}

    def pending(self) -> list[str]:
        return sorted(e.id for e in self._entries().values() if not e.reviewed)


def _problems_message(head: str, errors: list[str]) -> str:
    if len(errors) == 1:
        return f"{head} Problem: {errors[0]}"
    return head + " Problems: " + " ".join(f"({i}) {e}" for i, e in enumerate(errors, 1))
