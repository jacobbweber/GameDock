"""Setting schemas: which values a setting may have (0.6.3).

A catalog entry can describe the settings of its config files (``config_files[].schema``) and of its deploy-form
fields (``fields[].type`` / ``choices`` / ``min`` / ``max``). GameDock then refuses values the game would refuse, in
the web page, in the HTTP API and in every MCP write tool, and shows choices as dropdowns.

Why: a value that is not one of a game's allowed words (Enshrouded's ``"tombstoneMode": "Off"``) makes some games refuse
to start at all, while the container stays up. Nothing in a text file tells a person or an AI agent which words are
allowed, so the catalog has to.

A schema is a mapping from setting path to a spec::

    schema:
      gameSettings.tombstoneMode:          # JSON: dotted path (lists: userGroups[0].name); ini: the key (or [Section]key)
        type: enum                         # string | integer | number | boolean | enum (choices imply enum)
        choices:
          - AddBackpackMaterials           # a plain value, or {value, label, description, aliases}
          - { value: NoTombstone, label: Keep everything, description: Nothing is dropped on death. }
        default: AddBackpackMaterials
        description: What players drop when they die.
        restart_required: true
      gameSettings.playerHealthFactor: { type: number, min: 0.25, max: 4 }

Pure functions only (no Docker, no files).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .errors import CatalogError, ValidationError

TYPES = ("string", "integer", "number", "boolean", "enum")
SPEC_KEYS = ("type", "choices", "min", "max", "pattern", "pattern_hint", "description", "default", "restart_required",
             "label")
CHOICE_KEYS = ("value", "label", "description", "aliases")
MAX_SPECS = 400
MAX_CHOICES = 100
MAX_TEXT = 600
_INT_RE = re.compile(r"^[+-]?\d+$")
_NUM_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_PATH_RE = re.compile(r"^[^\n\r\0]{1,200}$")
_BOOL = ("true", "false")
# Setting names that usually take one of a fixed set of words. Used only for authoring warnings.
ENUM_LOOKING_RE = re.compile(r"(mode|difficulty|preset|penalty|style|type|frequency|amount|level|repercussion|"
                             r"modifier|region|season|gamemode|platform)s?$", re.I)


class SettingsInvalid(ValidationError):
    """One or more values are not allowed by the catalog's schema. `problems` is a list of plain dicts
    ({setting, value, message, allowed, type, min, max, file})."""

    def __init__(self, message: str, problems: list[dict]):
        super().__init__(message)
        self.problems = problems


@dataclass(frozen=True)
class Choice:
    value: str
    label: str = ""
    description: str = ""
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = {"value": self.value, "label": self.label or self.value}
        if self.description:
            d["description"] = self.description
        if self.aliases:
            d["aliases"] = list(self.aliases)
        return d


@dataclass(frozen=True)
class SettingSpec:
    path: str
    type: str = "string"
    choices: tuple[Choice, ...] = ()
    min: float | None = None
    max: float | None = None
    pattern: str = ""
    pattern_hint: str = ""
    description: str = ""
    default: str | None = None
    restart_required: bool | None = None
    label: str = ""
    extra: dict = field(default_factory=dict, compare=False)

    def to_dict(self) -> dict:
        d: dict = {"path": self.path, "type": self.type}
        if self.label:
            d["label"] = self.label
        if self.choices:
            d["choices"] = [c.to_dict() for c in self.choices]
        for k in ("min", "max"):
            v = getattr(self, k)
            if v is not None:
                d[k] = int(v) if float(v).is_integer() and self.type != "number" else v
        for k in ("pattern", "pattern_hint", "description"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.default is not None:
            d["default"] = self.default
        if self.restart_required is not None:
            d["restart_required"] = self.restart_required
        return d

    def to_yaml(self) -> dict:
        d = self.to_dict()
        d.pop("path")
        if self.choices:
            d["choices"] = [c.value if not (c.label or c.description or c.aliases) else
                            {k: v for k, v in (("value", c.value), ("label", c.label), ("description", c.description),
                                               ("aliases", list(c.aliases))) if v}
                            for c in self.choices]
        return d

    @property
    def allowed_values(self) -> list[str]:
        return [c.value for c in self.choices]

    def allowed_text(self) -> str:
        """Plain words: what a right value looks like."""
        if self.choices:
            return "one of " + ", ".join(c.value + (f" ({c.label})" if c.label and c.label != c.value else "")
                                         for c in self.choices)
        if self.type == "boolean":
            return "true or false"
        if self.type in ("integer", "number"):
            kind = "a whole number" if self.type == "integer" else "a number"
            if self.min is not None and self.max is not None:
                return f"{kind} from {_num(self.min)} to {_num(self.max)}"
            if self.min is not None:
                return f"{kind} of at least {_num(self.min)}"
            if self.max is not None:
                return f"{kind} of at most {_num(self.max)}"
            return kind
        if self.pattern_hint:
            return self.pattern_hint
        return "text"


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def _text(v: object, name: str, where: str) -> str:
    if v is None:
        return ""
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        raise CatalogError(f"{where}: '{name}' must be text")
    s = " ".join(str(v).split())
    if len(s) > MAX_TEXT:
        raise CatalogError(f"{where}: '{name}' is longer than {MAX_TEXT} characters")
    return s


def _scalar(v: object) -> str:
    """A YAML scalar as the text it would be in a settings file (YAML booleans as true/false)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v)) if abs(v) < 1e15 else repr(v)
    return str(v)


def _parse_choice(c: object, where: str) -> Choice:
    if isinstance(c, (str, int, float, bool)):
        return Choice(_scalar(c))
    if not isinstance(c, dict) or "value" not in c:
        raise CatalogError(f"{where}: each choice must be a value or a mapping with 'value' (and optional label, "
                           "description, aliases)")
    extra = sorted(str(k) for k in c if k not in CHOICE_KEYS)
    if extra:
        raise CatalogError(f"{where}: a choice has keys GameDock does not know: {', '.join(extra)} "
                           f"(allowed: {', '.join(CHOICE_KEYS)})")
    aliases = c.get("aliases") or []
    if not isinstance(aliases, list) or not all(isinstance(a, (str, int, float, bool)) for a in aliases):
        raise CatalogError(f"{where}: 'aliases' of a choice must be a list of values")
    return Choice(_scalar(c["value"]), _text(c.get("label"), "label", where), _text(c.get("description"), "description", where),
                  tuple(_scalar(a) for a in aliases))


def _bound(v: object, name: str, where: str) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise CatalogError(f"{where}: '{name}' must be a number")
    return float(v)


def parse_spec(path: str, raw: object, where: str) -> SettingSpec:
    """One setting spec from a catalog file. Raises CatalogError with a plain message when it is malformed."""
    w = f"{where}: setting {path!r}"
    if not isinstance(path, str) or not _PATH_RE.match(path) or path != path.strip():
        raise CatalogError(f"{where}: bad setting path {path!r} in schema")
    if isinstance(raw, str):
        raw = {"type": raw}
    if not isinstance(raw, dict):
        raise CatalogError(f"{w} must be a mapping such as {{type: enum, choices: [A, B]}}")
    extra = sorted(str(k) for k in raw if k not in SPEC_KEYS)
    if extra:
        raise CatalogError(f"{w} has keys GameDock does not know: {', '.join(extra)} (allowed: {', '.join(SPEC_KEYS)})")
    choices_raw = raw.get("choices")
    if choices_raw is not None and (not isinstance(choices_raw, list) or not choices_raw or len(choices_raw) > MAX_CHOICES):
        raise CatalogError(f"{w}: 'choices' must be a list of 1 to {MAX_CHOICES} values")
    raw_vals = [c.get("value") if isinstance(c, dict) else c for c in choices_raw or []]
    if any(isinstance(v, bool) for v in raw_vals) and any(isinstance(v, str) for v in raw_vals):
        raise CatalogError(f"{w}: a choice was read as true/false by YAML (off, on, yes and no are booleans in YAML). "
                           "Put such words in quotes, for example \"off\"")
    choices = tuple(_parse_choice(c, w) for c in choices_raw or [])
    seen: set[str] = set()
    for c in choices:
        for v in (c.value, *c.aliases):
            if v in seen:
                raise CatalogError(f"{w}: the value {v!r} is listed twice in 'choices'")
            seen.add(v)
    typ = str(raw.get("type") or ("enum" if choices else "string")).lower()
    if typ in ("bool",):
        typ = "boolean"
    if typ in ("int",):
        typ = "integer"
    if typ in ("float",):
        typ = "number"
    if typ not in TYPES:
        raise CatalogError(f"{w}: type must be one of {', '.join(TYPES)}, got {typ!r}")
    if typ == "enum" and not choices:
        raise CatalogError(f"{w}: type enum needs 'choices' (the values the game accepts)")
    if choices and typ not in ("enum", "integer", "number", "string"):
        raise CatalogError(f"{w}: 'choices' cannot be combined with type {typ}")
    if choices and typ in ("integer", "number"):
        for c in choices:
            if not (_INT_RE if typ == "integer" else _NUM_RE).match(c.value):
                raise CatalogError(f"{w}: choice {c.value!r} is not a {'whole number' if typ == 'integer' else 'number'}")
    if choices and typ == "string":
        typ = "enum"
    lo, hi = _bound(raw.get("min"), "min", w), _bound(raw.get("max"), "max", w)
    if (lo is not None or hi is not None) and typ not in ("integer", "number"):
        raise CatalogError(f"{w}: min and max only work with type integer or number")
    if lo is not None and hi is not None and lo > hi:
        raise CatalogError(f"{w}: min ({_num(lo)}) is larger than max ({_num(hi)})")
    pattern = raw.get("pattern") or ""
    if pattern:
        if not isinstance(pattern, str) or len(pattern) > 300 or "\n" in pattern:
            raise CatalogError(f"{w}: 'pattern' must be one line of at most 300 characters")
        try:
            re.compile(pattern)
        except re.error as e:
            raise CatalogError(f"{w}: 'pattern' is not a valid regular expression: {e}") from e
        if typ != "string":
            raise CatalogError(f"{w}: 'pattern' only works with type string")
    rr = raw.get("restart_required")
    if rr is not None and not isinstance(rr, bool):
        raise CatalogError(f"{w}: restart_required must be true or false")
    default = raw.get("default")
    spec = SettingSpec(path, typ, choices, lo, hi, pattern, _text(raw.get("pattern_hint"), "pattern_hint", w),
                       _text(raw.get("description"), "description", w),
                       None if default is None else _scalar(default), rr, _text(raw.get("label"), "label", w))
    if spec.default not in (None, ""):
        err = check_value(spec, spec.default)
        if err:
            raise CatalogError(f"{w}: the default {spec.default!r} is not allowed by its own rules ({spec.allowed_text()})")
    return spec


def parse_schema(raw: object, where: str) -> tuple[SettingSpec, ...]:
    if raw in (None, "", {}):
        return ()
    if not isinstance(raw, dict):
        raise CatalogError(f"{where}: 'schema' must be a mapping of setting path to its rules")
    if len(raw) > MAX_SPECS:
        raise CatalogError(f"{where}: 'schema' has {len(raw)} settings; at most {MAX_SPECS} are allowed")
    return tuple(parse_spec(str(k) if not isinstance(k, str) else k, v, where) for k, v in raw.items())


def schema_to_yaml(specs: tuple[SettingSpec, ...]) -> dict:
    return {s.path: s.to_yaml() for s in specs}


# ------------------------------------------------------------------ checking values
def matches_choice(spec: SettingSpec, value: str) -> Choice | None:
    for c in spec.choices:
        if value == c.value or value in c.aliases:
            return c
    if spec.type in ("integer", "number") and _NUM_RE.match(value.strip() or "x"):
        for c in spec.choices:
            try:
                if float(c.value) == float(value):
                    return c
            except ValueError:
                pass
    return None


def check_value(spec: SettingSpec, value: str) -> str | None:
    """None when `value` (the text as it would be written) is allowed, else a plain sentence saying why not."""
    v = "" if value is None else str(value)
    name = spec.label or spec.path
    if spec.choices:
        if matches_choice(spec, v) is not None:
            return None
        return f"{v!r} is not a valid value for {name}. Allowed: {', '.join(spec.allowed_values)}."
    if spec.type == "boolean":
        if v.strip().lower() in _BOOL:
            return None
        return f"{v!r} is not a valid value for {name}: it must be true or false."
    if spec.type in ("integer", "number"):
        s = v.strip()
        ok = (_INT_RE if spec.type == "integer" else _NUM_RE).match(s)
        if not ok and spec.type == "integer" and _NUM_RE.match(s):
            try:
                ok = float(s).is_integer()
            except ValueError:
                ok = False
        if not ok:
            return f"{v!r} is not a valid value for {name}: it must be {spec.allowed_text()}."
        n = float(s)
        if (spec.min is not None and n < spec.min) or (spec.max is not None and n > spec.max):
            return f"{v!r} is out of range for {name}: it must be {spec.allowed_text()}."
        return None
    if spec.pattern:
        try:
            if re.fullmatch(spec.pattern, v) is None:
                return f"{v!r} is not a valid value for {name}: {spec.pattern_hint or 'it does not have the expected form'}."
        except re.error:
            return None
    return None


def problem_dict(spec: SettingSpec, value: str, message: str, file: str = "", field_id: str = "") -> dict:
    d = {"setting": spec.path, "value": value, "message": message, "allowed": spec.allowed_values,
         "allowed_text": spec.allowed_text(), "type": spec.type}
    if spec.min is not None:
        d["min"] = spec.to_dict()["min"]
    if spec.max is not None:
        d["max"] = spec.to_dict()["max"]
    if file:
        d["file"] = file
    if field_id and field_id != spec.path:
        d["field_id"] = field_id
    return d


def find_spec(specs: tuple[SettingSpec, ...], field_id: str, key: str) -> SettingSpec | None:
    """The spec for a form field: by its id (``[Section]key``, a dotted JSON path) or by its bare key."""
    if not specs:
        return None
    base = field_id.split("#", 1)[0]
    for s in specs:
        if s.path == base:
            return s
    for s in specs:
        if s.path == key:
            return s
    m = re.match(r"^\[([^\]]+)\](.+)$", base)
    if m:
        dotted = f"{m.group(1)}.{m.group(2)}"
        for s in specs:
            if s.path == dotted:
                return s
    return None


def find_by_name(specs: tuple[SettingSpec, ...], name: str) -> SettingSpec | None:
    """The spec whose last path part equals `name`, ignoring case and separators (for names taken from a game's log,
    such as TombstoneMode -> gameSettings.tombstoneMode)."""
    norm = re.sub(r"[^a-z0-9]", "", name.lower())
    for s in specs:
        last = re.split(r"[.\]]", s.path)[-1]
        if re.sub(r"[^a-z0-9]", "", last.lower()) == norm:
            return s
    return None


def summary_message(problems: list[dict], where: str = "") -> str:
    head = f"Not saved: {len(problems)} setting{'s' if len(problems) != 1 else ''}" + (f" in {where}" if where else "") + \
        " would be refused by the game. "
    return head + " ".join(p["message"] for p in problems[:5]) + (" ..." if len(problems) > 5 else "")
