"""The service methods behind the "add a game" tools and the Review dialog (mixed into GameDockService)."""
from __future__ import annotations

import difflib
import json
from pathlib import Path

from . import authoring as au
from . import steam as steam_module
from .errors import BackendError, ConflictError, ValidationError
from .models import IMAGE_RE

REPO_ROOT = Path(__file__).resolve().parent.parent
PROCEDURE_PATH = REPO_ROOT / "docs" / "ADDING_A_GAME.md"
GUIDE_RESOURCE = "gamedock://add-a-game"

STEPS = (
    "Find a maintained Docker image for the game's dedicated server and read its documentation.",
    "Inspect the EXACT image tag you will use (inspect_image) and confirm the ports and their protocols (UDP or TCP).",
    "Confirm which folders hold the saves and the settings, and mount them.",
    "Identify every documented player-facing environment variable and which of them are secret; expose each as a field "
    "(or say in the notes why one was left out).",
    "Find the Steam app id of the dedicated server (not the game) and how the image updates the game with SteamCMD.",
    "Find the log lines that mean ready, show the version and show download progress.",
    "Write the entry (start from catalog/_template.yaml).",
    "Validate it (validate_catalog_entry) until it has no errors and no warnings you cannot explain, then add it "
    "(add_catalog_entry).",
    "Wait for a person to approve it in the web page (poll get_catalog_review_status), then preview_deploy and check_ports, "
    "and deploy a test copy on spare ports.",
    "Verify (verify_game_setup, get_server_logs, get_server_diagnostics), fix the entry with update_catalog_entry, ask for "
    "re-approval, redeploy the test copy, and report the result.",
)
FALLBACK_PROCEDURE = (
    "# Adding a game to GameDock (short version)\n\nThe full procedure is the file docs/ADDING_A_GAME.md in the GameDock "
    "repository; this copy is only shown because that file was not found next to the running program.\n\n"
    + "\n".join(f"{i}. {s}" for i, s in enumerate(STEPS, 1)) + "\n\nA person must approve every entry that an AI "
    "agent adds before it can be deployed. Never invent an image, a port or an app id: research them and say what you "
    "could not confirm in the entry's notes.\n")
FALLBACK_TEMPLATE = """# Template for a GameDock catalog entry. Replace every placeholder. Never invent values: research them.
id: my-game                      # lowercase letters, digits, - and _
name: My Game Dedicated Server
image: REGISTRY/NAME:TAG         # a maintained image; prefer a pinned tag
description: One line for the card.
notes: Where the information came from, and what has NOT been verified yet.
ports:
  - { protocol: udp, host: 27015, container: 27015 }
volumes:
  - { host: ./saves, container: /saves }
fields:
  - { name: SERVER_NAME, label: Server name, default: "My Server", required: true }
  - { name: SERVER_PASSWORD, label: Password, secret: true, description: Leave empty for no password. }
"""


class AuthoringMixin:
    def _init_authoring(self) -> None:
        self.authoring = au.CatalogAuthoring(self.catalog_dir, self.data_dir, entries=self._catalog,
                                             changed=self._catalog_changed)
        self.http_get = steam_module.default_http_get   # (url, timeout) -> (status, text); tests replace it

    def _catalog_changed(self) -> None:
        self._entries_cache = None

    def _audit_web(self, tool: str, args: dict, status: str = "ok", detail: str = "") -> None:
        hook = getattr(self, "audit_hook", None)
        if hook:
            try:
                hook(tool, {**args, "trigger": "web"}, status, detail)
            except Exception:  # noqa: BLE001 - auditing must never break the action
                pass

    # ---- review gate
    def require_reviewed(self, entry) -> None:
        if not entry.reviewed:
            raise ConflictError(
                f"'{entry.name}' (id {entry.id}) was added by an AI agent and has not been approved by a person yet, "
                "so it cannot be deployed. Open the Catalog page in the GameDock web page, press Review, read what "
                "the entry will do and press Approve. If the file was changed after it was approved, it needs to be "
                "approved again.")

    # ---- authoring (MCP) and review (web)
    def validate_catalog_entry(self, yaml_text: str) -> dict:
        return self.authoring.validate(yaml_text)

    def add_catalog_entry(self, yaml_text: str) -> dict:
        return self.authoring.add(yaml_text)

    def update_catalog_entry(self, yaml_text: str) -> dict:
        return self.authoring.update(yaml_text)

    def catalog_review(self, game_id: str) -> dict:
        return self.authoring.review_info(game_id)

    def catalog_review_status(self, game_id: str, include_yaml: bool = False) -> dict:
        """Read-only approval state of one entry, plus which deployed servers still run an older snapshot of it."""
        out = self.authoring.review_status(game_id, include_yaml)
        servers = []
        entry = self._catalog().get(game_id)
        for inst in self.store.all():
            if inst.game_id != game_id or entry is None:
                continue
            drift = self.template_drift(inst, entry)
            servers.append({"server_id": inst.id, "name": inst.name, "uses_current_entry": not drift,
                            "differences": drift})
        out["servers"] = servers
        stale = [x["server_id"] for x in servers if not x["uses_current_entry"]]
        if stale:
            out["servers_on_older_snapshot"] = stale
            out["servers_note"] = ("These servers were deployed from an older version of the entry and keep that snapshot "
                                   "(image, ports, folders, settings, config files). Changing the entry does not change "
                                   "them: delete and deploy again to use the current version.")
        return out

    def approve_catalog_entry(self, game_id: str, yaml_sha256: str) -> dict:
        r = self.authoring.approve(game_id, yaml_sha256)
        self._audit_web("approve_catalog_entry", {"game_id": game_id, "yaml_sha256": yaml_sha256})
        return r

    def reject_catalog_entry(self, game_id: str, confirm: bool = False) -> dict:
        r = self.authoring.reject(game_id, confirm)
        self._audit_web("reject_catalog_entry", {"game_id": game_id})
        return r

    def catalog_review_pending(self) -> list[str]:
        try:
            return self.authoring.pending()
        except Exception:  # noqa: BLE001 - the status banner must never fail because of the catalog
            return []

    # ---- guide
    def add_game_procedure(self) -> str:
        try:
            return PROCEDURE_PATH.read_text(encoding="utf-8")
        except OSError:
            return FALLBACK_PROCEDURE

    def add_game_template(self) -> str:
        for p in (self.catalog_dir / "_template.yaml", REPO_ROOT / "catalog" / "_template.yaml"):
            try:
                return p.read_text(encoding="utf-8")
            except OSError:
                continue
        return FALLBACK_TEMPLATE

    def add_game_guide(self) -> dict:
        return {
            "procedure_markdown": self.add_game_procedure(),
            "steps": list(STEPS),
            "field_reference": au.field_reference(),
            "template_yaml": self.add_game_template(),
            "limits": dict(au.LIMITS),
            "rules": [
                "Always inspect the exact image tag you use (inspect_image with that tag); never assume that 'latest' exists. "
                "validate_catalog_entry warns when the tag was never inspected.",
                "Expose every documented player-facing setting as a field, or list in the entry's notes why one was left out. "
                "validate_catalog_entry warns about image variables that are neither exposed nor fixed in env.",
                "Add, then wait: the entry cannot be deployed until a person approves it. Poll get_catalog_review_status "
                "(read-only) to see whether they did; it also says if the file changed after approval.",
                "After approval deploy a test copy on spare ports (preview_deploy and check_ports first), then run "
                "verify_game_setup. Fix what it reports with update_catalog_entry, ask for approval again, and redeploy: a "
                "deployed server keeps a snapshot of the entry, so catalog changes only reach a NEW deploy (ask the person to "
                "delete the test server, or use full mode).",
                "Never invent an image name, a port or a Steam app id. Research them from the image's own documentation; "
                "if you cannot confirm something, leave it out or say so in the entry's notes.",
                "Never put a password or token in the entry. Make it a field with secret: true.",
                "Entries you add are marked 'Added by AI, needs your review' and cannot be deployed until a person "
                "approves them in the web page. There is no tool to approve.",
                "Entries that came from the repository are never changed by MCP; only entries you added can be updated, "
                "and an update needs a new review.",
                "Only ports 1024 and above, relative host folders, and the documented keys are accepted. Privileged mode, "
                "host networking, devices, extra capabilities and the Docker socket are never allowed.",
            ],
            "tools": ["get_add_game_guide", "inspect_image", "validate_catalog_entry", "add_catalog_entry",
                      "update_catalog_entry", "get_catalog_review_status", "check_ports", "preview_deploy",
                      "verify_game_setup", "get_server_diagnostics"],
            "workflow": ["add_catalog_entry", "(person approves in the web page; poll get_catalog_review_status)",
                         "preview_deploy + check_ports", "deploy_server (test copy, spare ports)", "verify_game_setup",
                         "fix with update_catalog_entry -> re-approval -> redeploy", "report"],
            "resource": GUIDE_RESOURCE,
        }

    # ---- image inspection (read-only)
    def inspect_image(self, image: str) -> dict:
        image = (image or "").strip()
        if not IMAGE_RE.match(image) or ".." in image or "//" in image:
            raise ValidationError(f"'{image}' is not a normal image reference. Use name[:tag][@digest] with letters, "
                                  "digits and . _ - / : @ only, for example 'someuser/some-server:1.2'.")
        info = self.backend.inspect_image(image)   # may raise BackendError with a plain sentence
        out = _describe_image(info)
        missing = not out.get("local") and not out.get("remote")
        kind = out.get("remote_error_kind") or ""
        if missing and kind in ("not_found", "denied") and "@" not in image:
            tags = self._known_tags(image)
            out["tag_status"] = "not_found"
            out["available_tags"] = tags.get("tags", [])
            out["similar_tags"] = tags.get("similar", [])
            out["tag_listing"] = tags.get("note", "")
            out["summary"] = list(out["summary"]) + _tag_notes(image, tags)
        elif missing:
            out["tag_status"] = "unknown"
        else:
            out["tag_status"] = "found"
        out["ok"] = not missing
        out["failure_reason"] = (out.get("failure") or out.get("remote_error") or "") if missing else ""
        try:
            self.authoring.inspections.record(image, out)
        except Exception:  # noqa: BLE001 - remembering the inspection is a convenience
            pass
        return out

    # Docker Hub's public tag list (read-only). Other registries are not asked.
    _TAG_LIST_TIMEOUT = 6.0

    def _known_tags(self, image: str) -> dict:
        """Tags the registry lists for this image: {"tags": [...], "similar": [...], "note": why nothing / where from}."""
        ref = image.split("@", 1)[0]
        last = ref.rsplit("/", 1)[-1]
        name, wanted = (ref.rpartition(":")[0], last.split(":", 1)[1]) if ":" in last else (ref, "")
        first, _, rest = name.partition("/")
        if rest and ("." in first or ":" in first or first == "localhost"):
            return {"note": f"Tag lists are only available for Docker Hub images; {first} is another registry. Look at the "
                            "image's own page for its tags."}
        repo = name if "/" in name else f"library/{name}"
        if not getattr(self.steam, "enabled", True):
            return {"note": "Looking up tags online is switched off (GAMEDOCK_UPDATE_CHECK=false)."}
        url = f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size=30&ordering=last_updated"
        try:
            status, body = self.http_get(url, self._TAG_LIST_TIMEOUT)
        except Exception as e:  # noqa: BLE001 - offline or blocked: say so, do not fail the inspection
            return {"note": f"The tag list could not be fetched from Docker Hub ({type(e).__name__}); the machine may be offline."}
        if status == 404:
            return {"note": f"Docker Hub has no repository '{repo}': the image name itself looks wrong."}
        if status != 200:
            return {"note": f"Docker Hub's tag list was not available (HTTP {status})."}
        try:
            names = [str(r.get("name")) for r in (json.loads(body).get("results") or []) if isinstance(r, dict) and r.get("name")]
        except (ValueError, AttributeError):
            return {"note": "Docker Hub's tag list could not be read."}
        similar = difflib.get_close_matches(wanted, names, n=3, cutoff=0.4) if wanted else []
        return {"tags": names[:30], "similar": similar,
                "note": "Newest tags first, from Docker Hub." if names else "Docker Hub lists no tags for this image."}


def _tag_notes(image: str, tags: dict) -> list[str]:
    notes = []
    if tags.get("tags"):
        notes.append("Tags that exist for this image (newest first): " + ", ".join(tags["tags"][:20]) + ".")
        if tags.get("similar"):
            notes.append("Closest to the one you asked for: " + ", ".join(tags["similar"]) + ".")
        notes.append("Pick one of these exact tags and call inspect_image again with it.")
    elif tags.get("note"):
        notes.append(tags["note"])
    return notes


def _describe_image(info: dict) -> dict:
    image = info["image"]
    out = dict(info)
    notes: list[str] = []
    d = info.get("local_details")
    if info.get("local") and d:
        ports = d["exposed_ports"]
        notes.append("The image is stored on this machine, so these facts come from the image itself.")
        notes.append("It declares these ports: " + (", ".join(ports) if ports else "none (the documentation may list "
                     "more: images do not have to declare them)") + ".")
        notes.append("It declares these volumes (folders the game saves to): "
                     + (", ".join(d["volumes"]) if d["volumes"] else "none declared (read the documentation for the "
                                                                    "save folder)") + ".")
        notes.append(f"It runs as user: {d['user']}.")
        if d["user"].startswith("(not set"):
            notes.append("It runs as root unless the image has a PUID/PGID setting: check its documentation, because "
                         "the files it writes must be readable for backups.")
        notes.append(f"It knows {len(d['env_names'])} environment variable name(s): " + ", ".join(d["env_names"][:40])
                     + (" ..." if len(d["env_names"]) > 40 else "") + ". (Values are not shown. These names include "
                     "ones set by the base image, so check which ones the game's documentation actually uses.)")
        hc = d.get("healthcheck")
        notes.append("It has a health check (so GameDock can tell when it is healthy)." if hc else
                     "It has no health check, so use ready_log_pattern to tell when the game can be joined.")
    else:
        notes.append(
            "The image is NOT stored on this machine, so its ports, folders, user and environment names cannot be "
            "listed. GameDock does not download or run an image while an entry is being written. To see those facts, "
            f"ask the person to run `docker pull {image}` on the GameDock machine (or add the entry, have it approved, "
            "and deploy a test copy, which downloads the image), then call inspect_image again.")
    r = info.get("remote")
    if r:
        plat = r.get("platforms") or []
        notes.append("The registry knows this image" + (f" (digest {r['digest']})" if r.get("digest") else "")
                     + (". Platforms: " + ", ".join(plat) + "." if plat else "."))
        if plat and not any(p.startswith("linux/amd64") for p in plat):
            notes.append("WARNING: no linux/amd64 build is listed, so it will probably not run on a normal PC or server.")
    elif info.get("failure") or info.get("remote_error"):
        notes.append(info.get("failure") or info["remote_error"])
    out["summary"] = notes
    out["pulled_or_ran"] = False
    return out
