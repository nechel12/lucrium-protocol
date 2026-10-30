"""Чтение манифеста и JSON-схемы конфига из директории плагина.

Схема конфига — то, из чего панель **автоматически** рендерит форму настроек:
автор плагина не пишет фронтенд (спека §6.1, «блочность»).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lucrium_protocol.manifest import (
    MANIFEST_FILENAME,
    ManifestError,
    PluginManifest,
    parse_manifest_text,
)

__all__ = [
    "MANIFEST_FILENAME",
    "ConfigSchemaError",
    "ManifestError",
    "PluginManifest",
    "load_config_schema",
    "load_manifest",
    "parse_manifest_text",
]


class ConfigSchemaError(Exception):
    """``config_schema.json`` отсутствует, не парсится или не является объектом."""


def load_manifest(plugin_dir: str | Path) -> PluginManifest:
    """Читает и валидирует ``plugin.toml``; :class:`ManifestError` при проблемах."""
    path = Path(plugin_dir) / MANIFEST_FILENAME
    if not path.is_file():
        raise ManifestError([f"{MANIFEST_FILENAME} not found in {plugin_dir}"])
    return parse_manifest_text(path.read_text(encoding="utf-8"))


def load_config_schema(
    plugin_dir: str | Path,
    manifest: PluginManifest | None = None,
) -> dict[str, Any] | None:
    """Загружает JSON-схему конфига, на которую ссылается манифест.

    ``None``, если ``[config] schema`` не задан (настройки у плагина нет —
    панель не показывает форму). Ошибки — :class:`ConfigSchemaError`.
    """
    root = Path(plugin_dir).resolve()
    if manifest is None:
        manifest = load_manifest(root)
    if manifest.schema_path is None:
        return None

    schema_file = (root / manifest.schema_path).resolve()
    if not schema_file.is_relative_to(root):
        raise ConfigSchemaError(f"config schema escapes plugin directory: {manifest.schema_path}")
    if not schema_file.is_file():
        raise ConfigSchemaError(f"config schema not found: {manifest.schema_path}")
    try:
        data: Any = json.loads(schema_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigSchemaError(f"config schema is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigSchemaError("config schema must be a JSON object")
    if data.get("type", "object") != "object":
        raise ConfigSchemaError("config schema root type must be 'object'")
    return data
