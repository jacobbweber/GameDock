"""Turning service results into plain JSON-friendly dicts. Shared by the web and MCP layers so both
describe things identically. (Secrets are never in these results: the service strips them.)"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from .models import CatalogEntry, DeleteResult, ServerInfo


def server_to_dict(s: ServerInfo) -> dict:
    d = asdict(s)
    d["status"] = s.status.value
    return d


def catalog_to_dict(e: CatalogEntry) -> dict:
    return {
        "id": e.id, "name": e.name, "image": e.image, "description": e.description, "notes": e.notes,
        "ports": [asdict(p) for p in e.ports],
        "update_method": e.update_method, "update_note": e.update_note,
        "backup_stop_first": e.backup_stop_first, "save_command": list(e.save_command),
        "ready_log_pattern": e.ready_log_pattern, "progress_log_pattern": e.progress_log_pattern,
        "steam_app_id": e.steam_app_id, "steam_branch": e.steam_branch or ("public" if e.steam_app_id else ""),
        "version_log_pattern": e.version_log_pattern, "game_update_method": e.game_update_method,
        "fail_log_patterns": [{"pattern": f.pattern, "title": f.title, "advice": f.advice} for f in e.fail_log_patterns],
        "log_files": list(e.log_files), "builtin_fail_patterns": e.builtin_fail_patterns,
        "config_files": [{"path": c.path, "label": c.label, "format": c.format, "hot_edit": c.hot_edit,
                          "note": c.note} for c in e.config_files],
        "fields": [asdict(f) for f in e.fields],  # defaults of secret fields are not secret values
        # Where the entry came from. "mcp" = added by an AI agent; those cannot be deployed until a person approves.
        "origin": e.origin, "reviewed": e.reviewed, "deployable": e.reviewed,
        "review_note": "" if e.reviewed else "Added by AI, needs your review. It cannot be deployed until you approve it.",
        "source_file": e.source_file,
    }


def delete_result_to_dict(r: DeleteResult) -> dict:
    return {"deleted": r.deleted, "data_kept": not r.data_deleted, "data_deleted": r.data_deleted,
            "backup": r.backup.name if r.backup else None,
            "backup_location": r.backup.path if r.backup else None, "note": r.note}


def to_plain(obj: Any) -> Any:
    """Recursively turn dataclasses, enums and tuples into JSON-friendly values."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_plain(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    return obj
