"""Turn raw `docker` error text into plain-language messages with a suggested fix.

Pure functions, so they are easy to test without Docker."""
from __future__ import annotations

import re

from ..errors import BackendError, DockerUnavailableError

NOT_INSTALLED = ("The 'docker' command was not found. Install Docker Engine (https://docs.docker.com/engine/install/) "
                 "and make sure 'docker' is on the PATH of the user that runs GameDock.")
DAEMON_DOWN = ("Docker is installed but not running (or not reachable). Start it with: sudo systemctl start docker "
               "(and sudo systemctl enable docker to start it at boot).")
PERMISSION = ("This user is not allowed to use Docker. Add the user to the 'docker' group with: "
              "sudo usermod -aG docker $USER, then log out and back in (or restart the GameDock service).")


def classify(stderr: str) -> str | None:
    """'daemon_down', 'permission' or None, from docker's error text."""
    e = (stderr or "").lower()
    if "permission denied" in e and ("docker.sock" in e or "docker daemon" in e or "docker api" in e):
        return "permission"
    if ("cannot connect to the docker daemon" in e or "is the docker daemon running" in e
            or ("docker.sock" in e and ("no such file" in e or "connection refused" in e))
            or "error during connect" in e or "docker daemon is not running" in e):
        return "daemon_down"
    return None


def friendly(command: str, stderr: str) -> BackendError:
    """Build the exception for a failed `docker <command>`."""
    text = (stderr or "").strip()
    low = text.lower()
    kind = classify(text)
    if kind == "permission":
        return DockerUnavailableError(PERMISSION, "permission")
    if kind == "daemon_down":
        return DockerUnavailableError(DAEMON_DOWN, "daemon_down")
    if "port is already allocated" in low or "address already in use" in low or "ports are not available" in low:
        m = re.search(r"(?:0\.0\.0\.0|\[::\]|:::?):(\d+)", text) or re.search(r"port (\d+)", low)
        which = f"Host port {m.group(1)}" if m else "A host port"
        return BackendError(f"{which} is already in use by another program or container, so the server cannot "
                            "start. Pick another port (Edit the server) or stop the program that uses it.")
    if "no space left on device" in low:
        return BackendError("The disk is full (no space left on device). Free some space, then try again. "
                            "Docker's own storage is usually under /var/lib/docker.")
    if "pull access denied" in low or "repository does not exist" in low or "manifest unknown" in low \
            or "manifest for" in low and "not found" in low:
        return BackendError("Docker could not find that image (wrong name or tag, or it needs a login). "
                            f"Check the image name in the catalog entry. Details: {text}")
    if "toomanyrequests" in low or "rate limit" in low:
        return BackendError("The image registry is limiting downloads (rate limit). Wait a while, or run "
                            f"'docker login' as the GameDock user. Details: {text}")
    if "no matching manifest" in low or "exec format error" in low:
        return BackendError("This image is not built for this computer's CPU type (for example an x86 image on an "
                            f"ARM machine). Details: {text}")
    if "is already in use by container" in low or "conflict. the container name" in low:
        return BackendError("A container with this name already exists (it was probably not created by GameDock). "
                            f"Remove or rename it with the docker command, then try again. Details: {text}")
    if "cannot remove a running container" in low:
        return BackendError("The container is still running. Stop it first.")
    if "no such container" in low:
        return BackendError(f"The container does not exist ({text}). Start the server to create it again.")
    return BackendError(f"docker {command} failed: {text}" if text else f"docker {command} failed")


# Short problem / fix pairs for the doctor and the status banner.
SHORT = {
    "not_installed": ("The docker command is not installed (or not on PATH).",
                      "Install Docker Engine: https://docs.docker.com/engine/install/ . Then check: docker --version"),
    "daemon_down": ("Docker is installed but the Docker service is not running or not reachable.",
                    "sudo systemctl enable --now docker   (then check: docker ps)"),
    "permission": ("Docker is running, but this user is not allowed to use it.",
                   "sudo usermod -aG docker $USER   then log out and back in (or restart the GameDock service). "
                   "Check with: docker ps"),
    "error": ("Docker did not answer as expected.", "Run 'docker version' in a terminal and read what it says."),
}


# ---- what a failed registry question means (used by inspect_image so an agent gets a clear reason)
REGISTRY_NOT_FOUND, REGISTRY_DENIED, REGISTRY_RATE_LIMIT, REGISTRY_OFFLINE, REGISTRY_OTHER = (
    "not_found", "denied", "rate_limit", "offline", "other")


def classify_registry_error(text: str) -> str:
    """'not_found' (the name or tag does not exist), 'denied' (private image or login needed), 'rate_limit',
    'offline' (no network or DNS), or 'other', from the error text of `docker buildx imagetools inspect` or
    `docker manifest inspect`."""
    e = (text or "").lower()
    if "toomanyrequests" in e or "rate limit" in e:
        return REGISTRY_RATE_LIMIT
    if ("no such host" in e or "dial tcp" in e or "i/o timeout" in e or "network is unreachable" in e
            or "temporary failure in name resolution" in e or "context deadline exceeded" in e
            or "connection refused" in e or "no route to host" in e or "tls handshake timeout" in e):
        return REGISTRY_OFFLINE
    if ("not found" in e or "manifest unknown" in e or "name unknown" in e or "no such manifest" in e
            or "repository does not exist" in e or "does not exist" in e):
        return REGISTRY_NOT_FOUND
    if "unauthorized" in e or "denied" in e or "authentication required" in e or "requires authorization" in e:
        return REGISTRY_DENIED
    return REGISTRY_OTHER


def registry_failure(kind: str, image: str, detail: str = "") -> str:
    """The plain-words reason for a failed registry question."""
    tag = image.rsplit("/", 1)[-1].split(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] and "@" not in image else ""
    if kind == REGISTRY_NOT_FOUND:
        which = f"the tag '{tag}' of this image" if tag else "this image"
        return (f"The registry says {which} was not found: '{image}' does not exist there. Either the image name is "
                "wrong, or the tag is wrong (the tag 'latest' does not exist for every image).")
    if kind == REGISTRY_DENIED:
        return (f"The registry refused to answer about '{image}': it is private, or it needs a login, or the name is wrong "
                "(Docker Hub answers like this for names that do not exist).")
    if kind == REGISTRY_RATE_LIMIT:
        return "The registry is limiting requests right now (rate limit). Try again in a while."
    if kind == REGISTRY_OFFLINE:
        return "The registry could not be reached: the machine looks offline (no internet) or cannot resolve the registry's name."
    first = (detail.strip().splitlines() or ["no answer"])[0][:200]
    return f"The registry gave an answer GameDock does not understand ({first})."
