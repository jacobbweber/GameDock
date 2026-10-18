"""Tiny real git repositories for the update tests (no network: the 'remote' is a folder)."""
import subprocess
from pathlib import Path

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
       "GIT_TERMINAL_PROMPT": "0"}


def git(cwd, *args):
    import os
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env={**os.environ, **ENV})
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def write_version(path: Path, version: str) -> None:
    (path / "gamedock").mkdir(exist_ok=True)
    (path / "gamedock" / "__init__.py").write_text(f'"""x"""\n\n__version__ = "{version}"\n')


class Repos:
    """`remote` (what GitHub would hold) and `local` (the install folder, a clone of it)."""

    def __init__(self, tmp: Path, version: str = "0.3.0") -> None:
        self.remote_bare = tmp / "remote.git"
        self.seed = tmp / "seed"
        self.local = tmp / "install"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(self.remote_bare))
        git(tmp, "init", "-q", "-b", "main", str(self.seed))
        git(self.seed, "remote", "add", "origin", str(self.remote_bare))
        write_version(self.seed, version)
        (self.seed / "pyproject.toml").write_text("[project]\nname='x'\n")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", "first")
        git(self.seed, "push", "-q", "origin", "main")
        git(tmp, "clone", "-q", str(self.remote_bare), str(self.local))

    def remote_commit(self, version: str | None = None, files: dict | None = None, message: str = "change") -> str:
        if version:
            write_version(self.seed, version)
        for name, text in (files or {}).items():
            (self.seed / name).write_text(text)
        (self.seed / "marker.txt").write_text(message)
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", message)
        git(self.seed, "push", "-q", "origin", "main")
        return git(self.seed, "rev-parse", "HEAD")

    def local_commit(self, message: str = "local only") -> str:
        (self.local / "mine.txt").write_text(message)
        git(self.local, "add", "-A")
        git(self.local, "commit", "-q", "-m", message)
        return git(self.local, "rev-parse", "HEAD")

    def head(self, where="local") -> str:
        return git(self.local if where == "local" else self.seed, "rev-parse", "HEAD")
