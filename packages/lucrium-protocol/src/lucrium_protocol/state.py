"""Снимок состояния воркера — источник истины (спека §5, §8).

Воркер отдаёт ``StateSnapshot`` при подключении панели и по команде
``get_state``; далее поддерживает его актуальность событиями ``state``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .manifest import ManifestPermissions
from .types import AccountCredentials, AccountInput, Capability, Platform, ProxyConfig, SessionStatus, StatusCounts


class PluginStatus(StrEnum):
    """Состояние установленного плагина (host процессов)."""

    INSTALLED = "installed"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    CRASHED = "crashed"
    RESTARTING = "restarting"


class PluginSource(StrEnum):
    """Откуда плагин: v1 — только ``local`` (zip через панель); ``remote`` заложен под v2
    (доставка подписанных артефактов из дескриптора)."""

    LOCAL = "local"
    REMOTE = "remote"


class WorkerStatus(BaseModel):
    """Heartbeat (событие ``status``): uptime, здоровье, версии."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(description="версия воркера")
    protocol: str = Field(description="PROTOCOL_VERSION, который понимает воркер")
    started_at: datetime
    uptime_seconds: float = Field(ge=0)
    python: str = Field(description="например 3.14.7")
    os: str = Field(description="например linux-6.8.0-x86_64")
    sessions: StatusCounts = Field(default_factory=StatusCounts)
    plugins: StatusCounts = Field(default_factory=StatusCounts)
    connected_panels: int = Field(default=0, ge=0, description="сколько панелей подключено сейчас")


class AccountState(BaseModel):
    """Аккаунт + состояние его сессии (одна сессия на аккаунт)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, description="id аккаунта в воркере (= session_id)")
    platform: Platform
    label: str = Field(min_length=1, max_length=120)
    status: SessionStatus
    capabilities: list[Capability] = Field(
        default_factory=list,
        description="capability площадки из aiosellers — фронт скрывает недоступные действия",
    )
    proxy: ProxyConfig | None = None
    last_error: str | None = Field(default=None, max_length=2000)
    created_at: datetime
    updated_at: datetime
    last_event_at: datetime | None = Field(default=None, description="когда приходило последнее HubEvent")
    restarts: int = Field(default=0, ge=0, description="перезапусков сессии")


class PluginState(BaseModel):
    """Установленный плагин и его текущий процесс."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    version: str
    api: str = Field(description="версия контракта SDK из манифеста")
    source: PluginSource = PluginSource.LOCAL
    enabled: bool = True
    status: PluginStatus
    restarts: int = Field(default=0, ge=0, description="авто-restart'ов после крашей")
    last_error: str | None = Field(default=None, max_length=2000)
    has_config_schema: bool = Field(default=False, description="панель рендерит форму настроек, если True")
    config: dict[str, object] = Field(
        default_factory=dict, description="текущий конфиг (без секретов плагина — на усмотрение автора)"
    )
    permissions: ManifestPermissions | None = Field(
        default=None,
        description=(
            "разрешения, зафиксированные при установке: панель показывает их пользователю; "
            "None — запись создана старой версией воркера"
        ),
    )
    installed_at: datetime
    updated_at: datetime


class StateSnapshot(BaseModel):
    """Полный снапшот состояния воркера."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0, description="монотонный счётчик: панель игнорирует более старые снапшоты")
    generated_at: datetime
    status: WorkerStatus
    accounts: list[AccountState] = Field(default_factory=list)
    plugins: list[PluginState] = Field(default_factory=list)


class AccountWithCredentials(BaseModel):
    """Аккаунт с кредами — только в ответе ``reveal_credentials`` по явному запросу."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    account: AccountInput


class StateWithCredentials(BaseModel):
    """Result ``reveal_credentials``: снапшот **включая креды аккаунтов**.

    ``get_state`` и событие ``state`` отдают :class:`StateSnapshot` — без
    секретов. Раскрытие происходит только по явной команде авторизованной панели
    и пишется в лог воркера; до ``reveal_credentials`` креда уходили в каждом
    снапшоте, оседали в кэше панели и в истории сообщений (§8: панель не должна
    хранить важное).
    """

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    generated_at: datetime
    status: WorkerStatus
    accounts: list[AccountWithCredentials] = Field(default_factory=list)
    plugins: list[PluginState] = Field(default_factory=list)

    def credentials_by_account(self) -> dict[str, AccountCredentials]:
        """``account_id → AccountCredentials`` для воркера (пересоздание сессий aiosellers)."""
        return {entry.id: entry.account.credentials for entry in self.accounts}


__all__ = [
    "AccountState",
    "AccountWithCredentials",
    "PluginSource",
    "PluginState",
    "PluginStatus",
    "StateSnapshot",
    "StateWithCredentials",
    "WorkerStatus",
]
