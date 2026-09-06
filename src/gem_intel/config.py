"""Configuration loading.

YAML files under ``config/`` hold the defaults; environment variables
override them so the same image runs in dev and in CI without edits.

Override syntax: ``GEMINTEL_<PATH>`` with ``__`` between levels, e.g.
``GEMINTEL_DEADLINE__MAX_DAYS_REMAINING=14``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"
ENV_PREFIX = "GEMINTEL_"


class ConfigError(RuntimeError):
    pass


def _coerce(raw: str) -> Any:
    """Turn an env-var string into the most plausible Python value."""
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", ""}:
        return None
    for caster in (int, float):
        try:
            return caster(raw)
        except ValueError:
            pass
    if raw.lstrip().startswith(("[", "{")):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return raw


def _apply_env_overrides(data: dict[str, Any], environ: dict[str, str]) -> dict[str, Any]:
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX):].lower().split("__")
        if not path or not path[0]:
            continue
        cursor: dict[str, Any] = data
        for part in path[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[path[-1]] = _coerce(raw)
    return data


def _deep_get(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    cursor: Any = data
    for part in dotted.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor


@dataclass
class Settings:
    """Read-only view over merged settings + taxonomy."""

    raw: dict[str, Any] = field(default_factory=dict)
    taxonomy: dict[str, Any] = field(default_factory=dict)
    config_dir: Path = DEFAULT_CONFIG_DIR

    # -- generic access -------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        return _deep_get(self.raw, dotted, default)

    def require(self, dotted: str) -> Any:
        value = _deep_get(self.raw, dotted, _MISSING)
        if value is _MISSING:
            raise ConfigError(f"Required setting '{dotted}' is not configured")
        return value

    # -- frequently used, typed ----------------------------------------
    @property
    def timezone(self) -> str:
        return self.get("run.timezone", "Asia/Kolkata")

    @property
    def max_days_remaining(self) -> int:
        return int(self.get("deadline.max_days_remaining", 10))

    @property
    def allowed_hosts(self) -> set[str]:
        return {h.lower() for h in self.get("source.allowed_hosts", [])}

    @property
    def score_weights(self) -> dict[str, int]:
        return dict(self.get("scoring.weights", {}))

    @property
    def score_bands(self) -> list[dict[str, Any]]:
        return list(self.get("scoring.bands", []))

    @property
    def urgency_bands(self) -> list[dict[str, Any]]:
        return list(self.get("deadline.urgency_bands", []))

    @property
    def user_agent(self) -> str:
        template = self.get("source.user_agent", "GeM-IT-Tender-Intelligence/1.0")
        contact = os.getenv("GEM_CONTACT_EMAIL", "not-set@example.invalid")
        return template.replace("{contact_email}", contact)

    # -- taxonomy helpers ----------------------------------------------
    @property
    def product_groups(self) -> dict[str, dict[str, Any]]:
        return dict(self.taxonomy.get("products", {}))

    @property
    def service_groups(self) -> dict[str, dict[str, Any]]:
        return dict(self.taxonomy.get("services", {}))

    @property
    def all_groups(self) -> dict[str, dict[str, Any]]:
        merged = {}
        for name, group in self.product_groups.items():
            merged[name] = {**group, "_kind": "product"}
        for name, group in self.service_groups.items():
            merged[name] = {**group, "_kind": "service"}
        return merged

    def search_queries(self) -> list[str]:
        """Deduplicated, order-preserving list of every GeM search term."""
        seen: set[str] = set()
        out: list[str] = []
        for group in self.all_groups.values():
            for query in group.get("queries", []) or []:
                key = query.strip().lower()
                if key and key not in seen:
                    seen.add(key)
                    out.append(query.strip())
        return out

    @property
    def geo_terms(self) -> dict[str, list[str]]:
        geo = self.taxonomy.get("geography", {})
        return {
            "state_aliases": list(geo.get("state_aliases", [])),
            "districts": list(geo.get("districts", [])),
            "localities": list(geo.get("localities", [])),
            "local_org_hints": list(geo.get("local_org_hints", [])),
        }


class _Missing:
    pass


_MISSING = _Missing()


def load_settings(
    config_dir: Path | str | None = None,
    environ: dict[str, str] | None = None,
) -> Settings:
    """Load ``settings.yaml`` + ``taxonomy.yaml`` and apply env overrides."""
    directory = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    settings_path = directory / "settings.yaml"
    taxonomy_path = directory / "taxonomy.yaml"

    if not settings_path.exists():
        raise ConfigError(f"Missing settings file: {settings_path}")

    raw = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    taxonomy = (
        yaml.safe_load(taxonomy_path.read_text(encoding="utf-8")) or {}
        if taxonomy_path.exists()
        else {}
    )
    raw = _apply_env_overrides(raw, dict(environ if environ is not None else os.environ))
    return Settings(raw=raw, taxonomy=taxonomy, config_dir=directory)
