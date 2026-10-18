"""Lint the catalog folder: load every entry the way GameDock does and report what a person adding or reviewing a game
should look at. Used by scripts/lint_catalog.py and by the tests (so a broken catalog file fails the build).

  errors    the entry cannot be loaded (GameDock would skip it)
  warnings  loads, but something is probably wrong or risky (same checks as the Review dialog)
  notes     good to know
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import authoring as au
from .catalog import load_catalog
from .models import CatalogEntry

TEMPLATE = "_template.yaml"


@dataclass
class LintReport:
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self, show_notes: bool = False) -> str:
        lines = []
        for label, items in (("ERROR", self.errors), ("WARNING", self.warnings), *((("note", self.notes),) if show_notes else ())):
            for f, m in items:
                lines.append(f"{label:7} {f}: {m}")
        lines.append(f"Checked {len(self.checked)} catalog file(s): {len(self.errors)} error(s), "
                     f"{len(self.warnings)} warning(s), {len(self.notes)} note(s).")
        return "\n".join(lines)


def _extra(e: CatalogEntry) -> list[str]:
    """Checks that only make sense for the files in the repository."""
    out = []
    rel_hosts = [v.host.removeprefix("./").strip("/") for v in e.volumes if not v.host.startswith(("/", "~"))]
    for c in e.config_files:
        if rel_hosts and not any(c.path == h or c.path.startswith(h + "/") for h in rel_hosts if h):
            out.append(f"config file {c.path} is not inside any mounted folder ({', '.join(rel_hosts)}), so the game would "
                       "never see edits to it.")
    if e.installed_build_file and not e.steam_app_id:
        out.append("installed_build_file is set without steam_app_id, so it is never used.")
    if e.steam_manifest_dir and not e.steam_app_id:
        out.append("steam_manifest_dir is set without steam_app_id, so it is never used.")
    if e.game_update_method == "restart" and not e.steam_app_id:
        out.append("game_update_method is restart but steam_app_id is empty.")
    if e.update_method == "none" and not e.update_note:
        out.append("update_method is none but update_note does not explain how the game updates.")
    return out


def lint_catalog(directory: Path) -> LintReport:
    directory = Path(directory)
    report = LintReport()
    problems: list[tuple[str, str]] = []
    entries = load_catalog(directory, errors=problems)
    report.errors.extend(problems)
    for p in sorted(directory.iterdir()) if directory.is_dir() else []:
        if p.suffix in (".yaml", ".yml", ".json") and not p.name.startswith("_"):
            report.checked.append(p.name)
    template = directory / TEMPLATE
    if template.is_file():
        report.checked.append(template.name)
        c = au.check_text(template.read_text(encoding="utf-8"))
        for e in c.errors:
            report.errors.append((TEMPLATE, f"the template does not pass the entry checks: {e}"))
    for e in entries.values():
        f = e.source_file or f"{e.id}.yaml"
        for flag in au.analyze_entry(e, entries):
            (report.warnings if flag.level == "warn" else report.notes).append((f, flag.text))
        for m in _extra(e):
            report.warnings.append((f, m))
    return report
