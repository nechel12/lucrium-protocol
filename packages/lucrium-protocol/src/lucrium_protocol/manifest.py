"""Манифест плагина ``plugin.toml`` и его валидация (спека §6.1).

Пример:

.. code-block:: toml

    [plugin]
    id = "auto-reply-ai"
    name = "AI-автоответчик"
    version = "1.0.0"
    api = "1"                      # версия контракта SDK
    entry = "main"                 # модуль с on_start/on_event/on_stop

    [config]
    schema = "config_schema.json"  # JSON Schema — панель рендерит форму сама

    [permissions]
    accounts = ["funpay", "playerok"]
    network = true
    finance = false                # вывод/возвраты/кошельки — только явным opt-in

«Блочность»: панель рендерит форму настроек из ``schema`` автоматически —
автор плагина не пишет ни строки фронтенда.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .types import Platform
from .version import SUPPORTED_PLUGIN_API_VERSIONS

MANIFEST_FILENAME = "plugin.toml"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class ManifestError(Exception):
    """Манифест отсутствует, не парсится или не прошёл валидацию."""

    def __init__(self, issues: list[str]) -> None:
        super().__init__("; ".join(issues))
        self.issues = issues


class ManifestPlugin(BaseModel):
    """Секция ``[plugin]``."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=3, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=64)
    api: str = Field(default="1", max_length=16, description="версия контракта SDK (поле api)")
    entry: str = Field(default="main", max_length=200, description="модуль с on_start/on_event/on_stop")
    description: str | None = Field(default=None, max_length=4000)
    authors: list[str] = Field(default_factory=list)
    homepage: str | None = Field(default=None, max_length=500)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError("id must match ^[a-z0-9][a-z0-9_-]{2,63}$ (3-64 chars, lowercase, digits, '-', '_')")
        return v

    @field_validator("version")
    @classmethod
    def _valid_semver(cls, v: str) -> str:
        if not _SEMVER_RE.match(v):
            raise ValueError(f"version must be semver (got {v!r})")
        return v

    @field_validator("entry")
    @classmethod
    def _valid_entry(cls, v: str) -> str:
        if not _MODULE_RE.match(v):
            raise ValueError(f"entry must be a dotted python module path (got {v!r})")
        return v

    @field_validator("name")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v


class ManifestConfig(BaseModel):
    """Секция ``[config]``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_path: str | None = Field(
        default=None,
        alias="schema",
        max_length=200,
        description="путь к JSON Schema (относительно корня плагина); панель рендерит форму из неё",
    )

    @field_validator("schema_path")
    @classmethod
    def _valid_path(cls, v: str | None) -> str | None:
        if v is not None and (v.startswith("/") or "\\" in v or ".." in v):
            raise ValueError("schema path must be relative and inside the plugin directory")
        return v


class ManifestPermissions(BaseModel):
    """Секция ``[permissions]``."""

    model_config = ConfigDict(extra="forbid")

    accounts: list[Platform] = Field(
        default_factory=list,
        description="платформы, к сессиям которых допущен плагин (пусто = все установленные)",
    )
    network: bool = Field(
        default=False, description="разрешить ctx.http (прямая сеть из плагина запрещена по умолчанию)"
    )
    finance: bool = Field(
        default=False,
        description=(
            "разрешить финансовые вызовы клиента (finance.*, transactions.*, "
            "orders.refund, deals.cancel); по умолчанию запрещены"
        ),
    )


class PluginManifest(BaseModel):
    """Полный манифест ``plugin.toml``."""

    model_config = ConfigDict(extra="forbid")

    plugin: ManifestPlugin
    config: ManifestConfig = Field(default_factory=ManifestConfig)
    permissions: ManifestPermissions = Field(default_factory=ManifestPermissions)

    # удобные доступы
    @property
    def id(self) -> str:
        return self.plugin.id

    @property
    def name(self) -> str:
        return self.plugin.name

    @property
    def version(self) -> str:
        return self.plugin.version

    @property
    def api(self) -> str:
        return self.plugin.api

    @property
    def entry(self) -> str:
        return self.plugin.entry

    @property
    def schema_path(self) -> str | None:
        return self.config.schema_path


def parse_manifest_text(text: str) -> PluginManifest:
    """Парсит содержимое ``plugin.toml``; ошибки — :class:`ManifestError` с перечнем проблем."""
    issues: list[str] = []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError([f"invalid TOML: {exc}"]) from exc
    try:
        manifest = PluginManifest.model_validate(data)
    except ValueError as exc:
        # pydantic собирает все ошибки в ValidationError.errors()
        messages = []
        for err in getattr(exc, "errors", list)():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            messages.append(f"{loc}: {err.get('msg', '')}" if loc else str(err.get("msg", "")))
        raise ManifestError(messages or [str(exc)]) from exc
    issues.extend(validate_manifest(manifest))
    if issues:
        raise ManifestError(issues)
    return manifest


def load_manifest(plugin_dir: str | Path) -> PluginManifest:
    """Читает и валидирует ``plugin.toml`` из директории плагина."""
    path = Path(plugin_dir) / MANIFEST_FILENAME
    if not path.is_file():
        raise ManifestError([f"{MANIFEST_FILENAME} not found in {plugin_dir}"])
    return parse_manifest_text(path.read_text(encoding="utf-8"))


def validate_manifest(manifest: PluginManifest) -> list[str]:
    """Семантические проверки поверх pydantic-валидации. Возвращает список проблем ([] = ок)."""
    issues: list[str] = []
    if manifest.api not in SUPPORTED_PLUGIN_API_VERSIONS:
        supported = ", ".join(SUPPORTED_PLUGIN_API_VERSIONS)
        issues.append(f"plugin.api {manifest.api!r} is not supported (supported: {supported})")
    return issues


def ensure_valid_manifest(plugin_dir: str | Path) -> PluginManifest:
    """Алиас :func:`load_manifest` для читаемости в воркере."""
    return load_manifest(plugin_dir)


__all__ = [
    "MANIFEST_FILENAME",
    "ManifestConfig",
    "ManifestError",
    "ManifestPermissions",
    "ManifestPlugin",
    "PluginManifest",
    "ensure_valid_manifest",
    "load_manifest",
    "parse_manifest_text",
    "validate_manifest",
]
