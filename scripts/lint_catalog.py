"""Check every catalog file and print warnings (run it before committing a new game).

    .venv/bin/python scripts/lint_catalog.py [catalog folder] [--strict] [--notes]

Exit status 1 when an entry cannot be loaded (or, with --strict, when there are warnings). The tests run it too.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gamedock.lint import lint_catalog  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Lint GameDock catalog files.")
    ap.add_argument("folder", nargs="?", default=str(ROOT / "catalog"))
    ap.add_argument("--strict", action="store_true", help="warnings also give exit status 1")
    ap.add_argument("--notes", action="store_true", help="also print notes")
    args = ap.parse_args(argv)
    report = lint_catalog(Path(args.folder))
    print(report.render(args.notes))
    return 1 if report.errors or (args.strict and report.warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
