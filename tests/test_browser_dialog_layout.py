"""Browser test of the dialog layout (see scripts/check_dialog_layout.py for what is asserted and why).

Skipped when Playwright or a Chrome/Chromium is not available, or when GAMEDOCK_SKIP_BROWSER_TESTS=1. Set GAMEDOCK_CHROME to
the browser's path if it is not found automatically. About 20 seconds.
"""
import os
import shutil
import socket
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="Playwright is not installed (see requirements-dev.txt)")

if os.environ.get("GAMEDOCK_SKIP_BROWSER_TESTS") == "1":
    pytest.skip("GAMEDOCK_SKIP_BROWSER_TESTS=1", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_dialog_layout  # noqa: E402


def _chrome():
    for c in (os.environ.get("GAMEDOCK_CHROME"), shutil.which("google-chrome"), shutil.which("chromium"),
              shutil.which("chromium-browser"), "/usr/bin/google-chrome"):
        if c and Path(c).exists():
            return c
    return None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_dialogs_keep_one_fixed_frame_with_one_scroll_area():
    chrome = _chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium found (set GAMEDOCK_CHROME)")
    failures = check_dialog_layout.run(chrome=chrome, port=_free_port())
    assert not failures, "\n".join(failures)
