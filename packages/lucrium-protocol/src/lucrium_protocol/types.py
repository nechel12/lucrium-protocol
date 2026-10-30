"""Базовые типы протокола: платформы, capability, статусы, прокси, креды аккаунтов.

Зеркалит форк ``aiosellers-nechel12`` (``aiosellers.core``): ``Platform``,
``Capability``, ``EventKind``. Протокол **не зависит** от aiosellers —
воркер конвертирует объекты библиотеки в эти модели (см. ``docs/platforms.md``).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class Platform(StrEnum):
    """Площадки, поддерживаемые в v1."""

    FUNPAY = "funpay"
    PLAYEROK = "playerok"
    GGSEL = "ggsel"


class Capability(StrEnum):
    """Возможности площадки — зеркало ``aiosellers.Capability`` (Flag).

    Фронт панели по этому списку скрывает недоступные действия под конкретную
    площадку (спека §9). Неизвестные значения форка при конверсии отбрасываются.
    """

    MESSAGES = "messages"
    ORDERS = "orders"
    PURCHASES = "purchases"
    DEALS = "deals"
    REVIEWS = "reviews"
    LIST_MANAGEMENT = "list_management"
    IMAGE_UPLOAD = "image_upload"
    ITEM_PRIORITY = "item_priority"
    WEBSOCKET = "websocket"


def capabilities_from_names(names: Iterable[str]) -> list[Capability]:
    """Конвертирует имена capability (``"MESSAGES"``, ``Capability.MESSAGES`` и т.п.) в список.

    Неизвестные имена (новая версия форка ещё не описана в протоколе) отбрасываются —
    при расширении форка добавляйте значение в :class:`Capability` и версию протокола.
    """
    out: list[Capability] = []
    seen: set[Capability] = set()
    for raw in names:
        key = raw.strip().lower().removeprefix("capability.")
        try:
            cap = Capability(key)
        except ValueError:
            continue
        if cap not in seen:
            seen.add(cap)
            out.append(cap)
    return out


class EventKind(StrEnum):
    """Нормализованный kind события — зеркало ``aiosellers.EventKind``.

    Перенос «сырых» типов площадки в переносимые категории:

    * FunPay: ``NEW_MESSAGE`` → ``message``, ``NEW_ORDER``/``ORDER_STATUS_CHANGED`` → ``order``,
      ``NEW_PURCHASE`` → ``purchase``, списки → ``list``, инициализация → ``initial`` …
    * GGsel: ``NEW_MESSAGE`` → ``message``, ``NEW_SALE`` → ``order``, ``NEW_REVIEW`` → ``review`` …
    * PlayerOK: ``NEW_MESSAGE`` → ``message``, сделки (``NEW_DEAL``, ``ITEM_SENT`` …) → ``deal`` …
    """

    INITIAL = "initial"
    MESSAGE = "message"
    REVIEW = "review"
    ORDER = "order"
    PURCHASE = "purchase"
    DEAL = "deal"
    LIST = "list"
    OTHER = "other"


class SessionStatus(StrEnum):
    """Состояние сессии площадки в воркере."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ProxyScheme(StrEnum):
    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"


class ProxyConfig(BaseModel):
    """Прокси аккаунта.

    В ``aiosellers`` прокси передаётся строкой URL (``*.ClientConfig.proxy``);
    воркер собирает URL через :meth:`to_url`. Structured-форма нужна панели
    (адаптивная форма) и валидации.
    """

    model_config = ConfigDict(extra="forbid")

    scheme: ProxyScheme = ProxyScheme.HTTP
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: str | None = Field(default=None, max_length=255, repr=False)
    password: SecretStr | None = Field(default=None, repr=False)

    @field_validator("host")
    @classmethod
    def _host_sane(cls, v: str) -> str:
        if " " in v or "/" in v or "@" in v:
            raise ValueError("proxy host must be a bare hostname/IP")
        return v.lower()

    def to_url(self, *, with_credentials: bool = True) -> str:
        """URL для ``aiosellers`` (``scheme://[user:pass@]host:port``)."""
        auth = ""
        if with_credentials and self.username:
            password = self.password.get_secret_value() if self.password else ""
            from urllib.parse import quote

            auth = f"{quote(self.username, safe='')}:{quote(password, safe='')}@"
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{self.scheme}://{auth}{host}:{self.port}"

    @classmethod
    def from_url(cls, url: str) -> ProxyConfig:
        parsed = urlparse(url)
        if not parsed.hostname or parsed.port is None:
            raise ValueError(f"proxy url must be scheme://host:port, got {url!r}")
        try:
            scheme = ProxyScheme(parsed.scheme)
        except ValueError as exc:
            raise ValueError(f"unsupported proxy scheme {parsed.scheme!r}") from exc
        return cls(
            scheme=scheme,
            host=parsed.hostname,
            port=parsed.port,
            username=parsed.username,
            password=SecretStr(parsed.password) if parsed.password is not None else None,
        )


class AccountCredentials(BaseModel):
    """Креды площадки (спека §4): FunPay — ``golden_key``, PlayerOK — ``access_token``,
    GGsel — ``api_key`` + ``seller_id``.

    Секреты — ``SecretStr``: маскируются в repr/логах; на проводе передаются
    открыто (канал панель ↔ воркер — bearer-authenticated WSS; см. §10 спеки
    про опциональный TLS).
    """

    model_config = ConfigDict(extra="forbid")

    platform: Platform
    golden_key: SecretStr | None = Field(default=None, repr=False)
    access_token: SecretStr | None = Field(default=None, repr=False)
    api_key: SecretStr | None = Field(default=None, repr=False)
    seller_id: str | None = Field(default=None, max_length=64, description="только GGsel")

    @model_validator(mode="after")
    def _per_platform_fields(self) -> AccountCredentials:
        def secret(value: SecretStr | None) -> bool:
            return value is not None and bool(value.get_secret_value().strip())

        if self.platform is Platform.FUNPAY:
            if not secret(self.golden_key):
                raise ValueError("funpay requires golden_key")
            self._reject(self.access_token, self.api_key, self.seller_id)
        elif self.platform is Platform.PLAYEROK:
            if not secret(self.access_token):
                raise ValueError("playerok requires access_token")
            self._reject(self.golden_key, self.api_key, self.seller_id)
        else:  # GGSEL
            if not secret(self.api_key):
                raise ValueError("ggsel requires api_key")
            if not self.seller_id or not self.seller_id.strip():
                raise ValueError("ggsel requires seller_id")
            if not self.seller_id.strip().isdigit():
                raise ValueError("ggsel seller_id must be numeric")
            self._reject(self.golden_key, self.access_token)
        return self

    @staticmethod
    def _reject(*values: object) -> None:
        for value in values:
            if isinstance(value, SecretStr):
                if value.get_secret_value().strip():
                    raise ValueError("credentials for another platform must not be set")
            elif value is not None and str(value).strip():
                raise ValueError("credentials for another platform must not be set")

    def secret_value(self) -> str:
        """Открытая строка секрета для платформы (для воркера, не для логов)."""
        if self.platform is Platform.FUNPAY:
            assert self.golden_key is not None
            return self.golden_key.get_secret_value()
        if self.platform is Platform.PLAYEROK:
            assert self.access_token is not None
            return self.access_token.get_secret_value()
        assert self.api_key is not None
        return self.api_key.get_secret_value()


class AccountInput(BaseModel):
    """Тело команд ``add_account`` / базовая форма аккаунта."""

    model_config = ConfigDict(extra="forbid")

    platform: Platform
    label: str = Field(min_length=1, max_length=120, description="человекочитаемое имя в панели")
    credentials: AccountCredentials
    proxy: ProxyConfig | None = None

    @model_validator(mode="after")
    def _platforms_match(self) -> AccountInput:
        if self.credentials.platform is not self.platform:
            raise ValueError("credentials.platform must match account platform")
        return self


class StatusCounts(BaseModel):
    """Сводка сессий/плагинов для heartbeat."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(default=0, ge=0)
    running: int = Field(default=0, ge=0)
    error: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> StatusCounts:
        if self.running + self.error > self.total:
            raise ValueError("running + error must be <= total")
        return self


class LogEntry(BaseModel):
    """Запись журнала: и результат ``get_logs``, и данные события ``plugin_log``."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0, description="монотонный курсор для пагинации get_logs")
    ts: datetime = Field(description="UTC")
    level: LogLevel
    source: str = Field(min_length=1, max_length=200, description='"worker" или plugin_id')
    message: str = Field(max_length=8192)
    fields: dict[str, Any] = Field(default_factory=dict)


def to_wire(model: BaseModel) -> dict[str, Any]:
    """Сериализует модель для отправки по проводу, **раскрывая** ``SecretStr``.

    Pydantic маскирует ``SecretStr`` (``"**********"``) даже в JSON-режиме —
    этого мало панели, которая отправляет креды/пароли воркеру. Используется
    в ``build_request``; для ответов/событий (воркер → панель) секреты остаются
    замаскированными — панель ничего важного о себе не хранит.
    """

    def unwrap(value: Any) -> Any:
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        if isinstance(value, dict):
            return {key: unwrap(item) for key, item in value.items()}
        if isinstance(value, list):
            return [unwrap(item) for item in value]
        if isinstance(value, tuple):
            return tuple(unwrap(item) for item in value)
        return value

    return unwrap(model.model_dump(mode="python"))  # type: ignore[no-any-return]


__all__ = [
    "AccountCredentials",
    "AccountInput",
    "Capability",
    "EventKind",
    "LogEntry",
    "LogLevel",
    "Platform",
    "ProxyConfig",
    "ProxyScheme",
    "SessionStatus",
    "StatusCounts",
    "capabilities_from_names",
    "to_wire",
]
