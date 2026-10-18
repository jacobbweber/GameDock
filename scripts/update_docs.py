"""Rewrite the generated field reference table inside docs/ADDING_A_GAME.md from gamedock/authoring.py.

    .venv/bin/python scripts/update_docs.py          # rewrite the table
    .venv/bin/python scripts/update_docs.py --check  # exit 1 if the doc is out of date (the tests do the same)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gamedock.authoring import field_reference_markdown  # noqa: E402

DOC = ROOT / "docs" / "ADDING_A_GAME.md"
START, END = "<!-- field-reference:start -->", "<!-- field-reference:end -->"


def render(text: str) -> str:
    if START not in text or END not in text:
        raise SystemExit(f"{DOC} must contain {START} and {END}")
    head, _, rest = text.partition(START)
    _, _, tail = rest.partition(END)
    return f"{head}{START}\n{field_reference_markdown()}\n{END}{tail}"


if __name__ == "__main__":
    current = DOC.read_text(encoding="utf-8")
    new = render(current)
    if "--check" in sys.argv:
        sys.exit(0 if new == current else 1)
    DOC.write_text(new, encoding="utf-8")
    print("updated" if new != current else "already up to date")
