"""Load parliament.yaml — every country-specific value in one place.

Lives at the repository root rather than under ``backend/`` so the ingest CLI and
the maintenance scripts can import it without pulling in FastAPI.

    from parliament import PARLIAMENT
    PARLIAMENT.language.fts_config      # 'swedish'
    PARLIAMENT.party_color('S')         # '#E8112d'
    PARLIAMENT.vocabulary['speech']     # 'anförande'

Mirrors the loading style already used by backend/services/provider_registry.py:
a module-level singleton, ``yaml.safe_load``, frozen dataclasses.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Optional

import yaml

_ROOT = Path(__file__).resolve().parent

# Site copy — the explainer, the limit warning, the user guide. Overridable so a
# deployment's own wording lives outside the repository, the same way PROMPTS_DIR
# works for prompts.
def _resolve_path(value: Optional[str], default: Path) -> Path:
    """Resolve a configured path, treating a relative one as repo-relative.

    Without this, `PARLIAMENT_CONFIG=parliament.no.yaml` works when you happen to be
    standing in the repository and fails everywhere else — including under systemd.
    """
    if not value:
        return default
    path = Path(value)
    return path if path.is_absolute() else (_ROOT / path)


CONTENT_DIR = _resolve_path(os.environ.get("CONTENT_DIR"), _ROOT / "content")

# A Postgres text-search configuration name. It is interpolated into SQL rather
# than passed as a parameter (identifiers cannot be bound), so it is validated
# on load and never trusted from arbitrary input.
_FTS_CONFIG_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class ConfigError(ValueError):
    """parliament.yaml is missing, malformed, or internally inconsistent."""


def _as_str(value: Any) -> Any:
    """Undo YAML 1.1's boolean coercion for values that are meant to be text.

    YAML reads `no`, `yes`, `on`, `off`, `y` and `n` as booleans. That silently turns
    Norway's `country: NO` and `prompt_language: no` into False, and a one-letter party
    code like `N` into the same. Quoting in the file also works, but nobody remembers
    to, and the failure is a TypeError deep in a path join rather than anything that
    names the cause.
    """
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value


@dataclass(frozen=True)
class Party:
    code: str
    name: str
    color: str
    active: bool = True


@dataclass(frozen=True)
class Language:
    fts_config: str
    prompt_language: str
    locale: str
    preserve_characters: str
    name: str = ""            # the language's own name, e.g. "svenska"
    name_en: str = ""         # its English name, e.g. "Swedish"
    months: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Embeddings:
    model: str
    dimension: int
    base_url_env: str
    chunk_chars: int


@dataclass(frozen=True)
class Parliament:
    """The active parliament's configuration."""

    meta: dict[str, Any]
    language: Language
    vocabulary: dict[str, str]
    parties: list[Party]
    party_defaults: dict[str, str]
    activity_types: dict[str, dict[str, str]]
    document_subtypes: dict[str, str]
    decisions: dict[str, dict[str, str]]
    sessions: dict[str, Any]
    ids: dict[str, dict[str, str]]
    urls: dict[str, str]
    sources: dict[str, Any]
    embeddings: Embeddings
    site: dict[str, Any]
    theme: dict[str, str]
    path: Path

    # -- lookups ------------------------------------------------------------

    @cached_property
    def _by_code(self) -> dict[str, Party]:
        return {p.code: p for p in self.parties}

    def party(self, code: Optional[str]) -> Optional[Party]:
        return self._by_code.get((code or "").strip().upper())

    def party_color(self, code: Optional[str]) -> str:
        """Colour for a party code, or the neutral colour for unknown/independent."""
        found = self.party(code)
        return found.color if found else self.party_defaults["unknown_color"]

    def party_highlight_color(self, code: Optional[str], amount: float = 0.75) -> str:
        """A pale tint of the party colour, for text highlighting.

        Computed rather than configured — the predecessor kept a second
        hand-maintained table of lightened colours that could drift from the first.
        """
        found = self.party(code)
        if not found:
            return "#f0f0f0"
        r, g, b = (int(found.color.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
        blend = lambda c: round(c + (255 - c) * amount)  # noqa: E731
        return f"#{blend(r):02x}{blend(g):02x}{blend(b):02x}"

    @cached_property
    def party_codes(self) -> list[str]:
        return [p.code for p in self.parties if p.active]

    def activity_title(self, code: Optional[str]) -> str:
        return self.activity_types.get(code or "", {}).get("title", code or "")

    def person_photo_url(self, person_id: str) -> str:
        return self.urls["person_photo"].format(person_id=person_id)

    def session_label(self, start_year: int) -> str:
        """Render a session label, e.g. 2022 -> "2022/23"."""
        return self.sessions["label_format"].format(
            start=start_year, end_short=f"{(start_year + 1) % 100:02d}", end=start_year + 1
        )

    def read_content(self, key: str) -> str:
        """Read one of the markdown files referenced under `site:`."""
        rel = self.site.get(key)
        if not rel:
            return ""
        path = CONTENT_DIR / rel
        return path.read_text(encoding="utf-8") if path.exists() else ""

    # -- serialisation ------------------------------------------------------

    def public_meta(self) -> dict[str, Any]:
        """The payload served at GET /api/meta.

        Party colours ship to the client so the stylesheet does not have to
        hardcode one rule per party — which is what made the previous CSS
        Sweden-only in a way no configuration could fix.
        """
        return {
            "parliament": {
                "name": self.meta.get("name"),
                "name_en": self.meta.get("name_en"),
                "country": self.meta.get("country"),
                "data_start_year": self.meta.get("data_start_year"),
            },
            "parties": [
                {"code": p.code, "name": p.name, "color": p.color, "active": p.active}
                for p in self.parties
            ],
            "party_defaults": self.party_defaults,
            "theme": self.theme,
            "activity_types": self.activity_types,
            "vocabulary": self.vocabulary,
            "urls": self.urls,
            "site": {
                **{k: v for k, v in self.site.items() if not k.endswith("_file")},
                "explainer": self.read_content("explainer_file"),
                "limit_warning": self.read_content("limit_warning_file"),
            },
        }


def _require(data: dict, key: str) -> Any:
    if key not in data:
        raise ConfigError(f"parliament.yaml is missing the required `{key}:` section")
    return data[key]


def load(path: Optional[Path] = None) -> Parliament:
    """Read and validate a parliament configuration."""
    path = Path(path) if path else _resolve_path(
        os.environ.get("PARLIAMENT_CONFIG"), _ROOT / "parliament.yaml"
    )
    if not path.exists():
        raise ConfigError(
            f"No parliament configuration at {path}. Copy parliament.yaml from the "
            f"repository root, or set PARLIAMENT_CONFIG to point at yours."
        )

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    lang_raw = dict(_require(data, "language"))
    for key in ("prompt_language", "locale", "fts_config", "name", "name_en"):
        if key in lang_raw:
            lang_raw[key] = _as_str(lang_raw[key])
    lang = Language(**lang_raw)
    if not _FTS_CONFIG_RE.match(lang.fts_config):
        raise ConfigError(
            f"language.fts_config {lang.fts_config!r} is not a valid Postgres "
            f"identifier. It is interpolated into SQL, so it must match "
            f"{_FTS_CONFIG_RE.pattern}. List valid names with: "
            f"SELECT cfgname FROM pg_ts_config;"
        )

    parties = [Party(**{**p, "code": _as_str(p.get("code"))}) for p in data.get("parties", [])]
    if not parties:
        raise ConfigError("parliament.yaml declares no parties")

    embeddings = Embeddings(**_require(data, "embeddings"))
    if embeddings.dimension <= 0:
        raise ConfigError("embeddings.dimension must be a positive integer")

    meta = dict(_require(data, "parliament"))
    for key in ("id", "country", "chamber", "name", "name_en"):
        if key in meta:
            meta[key] = _as_str(meta[key])

    return Parliament(
        meta=meta,
        language=lang,
        vocabulary=data.get("vocabulary", {}),
        parties=parties,
        party_defaults=data.get("party_defaults", {"unknown_color": "#9aa5b8"}),
        activity_types=data.get("activity_types", {}),
        document_subtypes=data.get("document_subtypes", {}),
        decisions=data.get("decisions", {}),
        sessions=data.get("sessions", {}),
        ids=data.get("ids", {}),
        urls=data.get("urls", {}),
        sources=data.get("sources", {}),
        embeddings=embeddings,
        site=data.get("site", {}),
        theme=data.get("theme", {}),
        path=path,
    )


PARLIAMENT: Parliament = load()
