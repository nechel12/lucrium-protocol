"""Команды «панель → воркер» и их result-модели (спека §5).

Каждый payload команды несёт ``method`` (дискриминатор envelope) и типизированные
поля; ``COMMAND_RESULTS`` связывает метод с моделью ``result`` — по ней
``lucrium_protocol.envelope.decode_result`` парсит ответ воркера.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .errors import ErrorCode, ProtocolError
from .state import PluginState, StateWithCredentials
from .types import AccountCredentials, AccountInput, LogEntry, LogLevel, ProxyConfig, SessionStatus, to_wire

# максимум на поля с base64 (архив zip, дамп состояния): ~48 МБ сырого на 64 М символов
MAX_B64_CHARS = 64 * 1024 * 1024
MAX_RAW_BYTES = 48 * 1024 * 1024


class Command(StrEnum):
    """Полный список команд панели (спека §5)."""

    GET_STATE = "get_state"
    ADD_ACCOUNT = "add_account"
    UPDATE_ACCOUNT = "update_account"
    REMOVE_ACCOUNT = "remove_account"
    START_SESSION = "start_session"
    STOP_SESSION = "stop_session"
    INSTALL_PLUGIN = "install_plugin"
    UPDATE_PLUGIN = "update_plugin"
    REMOVE_PLUGIN = "remove_plugin"
    SET_PLUGIN_CONFIG = "set_plugin_config"
    EXPORT_STATE = "export_state"
    IMPORT_STATE = "import_state"
    GET_LOGS = "get_logs"
    ROTATE_TOKEN = "rotate_token"
    RESTART = "restart"
    PING = "ping"


class ImportMode(StrEnum):
    MERGE = "merge"
    REPLACE = "replace"


class LogScope(StrEnum):
    WORKER = "worker"
    PLUGIN = "plugin"


class RestartScope(StrEnum):
    WORKER = "worker"
    SESSIONS = "sessions"


def decode_b64(value: str, *, field: str) -> bytes:
    """Декодирует base64 с лимитом размера; ошибки — ``INVALID_PARAMS``."""
    if len(value) > MAX_B64_CHARS:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, f"{field} exceeds size limit")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, f"{field} is not valid base64: {exc}") from exc
    if len(raw) > MAX_RAW_BYTES:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, f"{field} exceeds size limit")
    return raw


# --- params ------------------------------------------------------------------


class GetStateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["get_state"] = "get_state"


class AddAccountParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["add_account"] = "add_account"
    account: AccountInput


class UpdateAccountParams(BaseModel):
    """Частичное обновление: пустые поля не трогают; ``clear_proxy`` снимает прокси."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["update_account"] = "update_account"
    account_id: str = Field(min_length=1, max_length=64)
    label: str | None = Field(default=None, min_length=1, max_length=120)
    credentials: AccountCredentials | None = None
    proxy: ProxyConfig | None = None
    clear_proxy: bool = False

    @model_validator(mode="after")
    def _sane_patch(self) -> UpdateAccountParams:
        if self.proxy is not None and self.clear_proxy:
            raise ValueError("proxy and clear_proxy are mutually exclusive")
        if not any([self.label is not None, self.credentials is not None, self.proxy is not None, self.clear_proxy]):
            raise ValueError("nothing to update: set at least one field")
        return self


class AccountIdParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)


class RemoveAccountParams(AccountIdParams):
    method: Literal["remove_account"] = "remove_account"


class StartSessionParams(AccountIdParams):
    method: Literal["start_session"] = "start_session"


class StopSessionParams(AccountIdParams):
    method: Literal["stop_session"] = "stop_session"


class InstallPluginParams(BaseModel):
    """v1: плагин устанавливается zip-архивом через панель (спека §6.2)."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["install_plugin"] = "install_plugin"
    archive_b64: str = Field(max_length=MAX_B64_CHARS, description="zip с plugin.toml в корне")
    sha256: str | None = Field(default=None, min_length=64, max_length=64, description="hex, контрольная сумма zip")
    overwrite: bool = False
    auto_start: bool = True


class UpdatePluginParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["update_plugin"] = "update_plugin"
    plugin_id: str = Field(min_length=1, max_length=200)
    archive_b64: str = Field(max_length=MAX_B64_CHARS)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    auto_restart: bool = True


class RemovePluginParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["remove_plugin"] = "remove_plugin"
    plugin_id: str = Field(min_length=1, max_length=200)
    purge_data: bool = Field(default=False, description="удалить и KV storage плагина")


class SetPluginConfigParams(BaseModel):
    """Конфиг, собранный панелью из JSON-схемы манифеста; воркер валидирует по схеме."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["set_plugin_config"] = "set_plugin_config"
    plugin_id: str = Field(min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)


class ExportStateParams(BaseModel):
    """Экспорт всего состояния в один файл, зашифрованный паролем пользователя (спека §4)."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["export_state"] = "export_state"
    passphrase: SecretStr = Field(min_length=8, max_length=1024)
    include_history: bool = Field(default=False, description="включить историю событий (ретеншн)")


class ImportStateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["import_state"] = "import_state"
    blob_b64: str = Field(max_length=MAX_B64_CHARS)
    passphrase: SecretStr = Field(min_length=8, max_length=1024)
    mode: ImportMode = ImportMode.MERGE


class GetLogsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["get_logs"] = "get_logs"
    scope: LogScope = LogScope.WORKER
    plugin_id: str | None = Field(default=None, max_length=200, description="обязателен при scope=plugin")
    level: LogLevel | None = Field(default=None, description="минимальный уровень")
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: int | None = Field(default=None, ge=0, description="возвращать записи с seq > cursor")

    @model_validator(mode="after")
    def _plugin_scope_needs_id(self) -> GetLogsParams:
        if self.scope is LogScope.PLUGIN and not self.plugin_id:
            raise ValueError("plugin_id is required when scope=plugin")
        return self


class RotateTokenParams(BaseModel):
    """Новый pairing-токен; старый отзывается. Панель показывает токен один раз."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["rotate_token"] = "rotate_token"


class RestartParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["restart"] = "restart"
    scope: RestartScope = RestartScope.WORKER
    delay_seconds: float = Field(default=0.0, ge=0, le=3600)


# --- results -----------------------------------------------------------------


class AccountRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str


class SessionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    status: SessionStatus


class PluginRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin_id: str


class PluginConfigResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin_id: str
    restart_triggered: bool = Field(description="плагин перезапущен с новым конфигом")


class ExportStateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(description="например lucrium-state-2026-01-15.lcs")
    blob_b64: str = Field(max_length=MAX_B64_CHARS)
    sha256: str = Field(min_length=64, max_length=64, description="hex от шифроблоба")
    size_bytes: int = Field(ge=0)


class ImportStateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ImportMode
    accounts: int = Field(ge=0)
    plugins: int = Field(ge=0)
    sessions_started: int = Field(ge=0)


class GetLogsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[LogEntry] = Field(default_factory=list)
    next_cursor: int | None = Field(default=None, description="передать как cursor в следующем запросе")


class RotateTokenResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    rotated_at: datetime


class RestartResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled: bool
    delay_seconds: float = Field(ge=0)


class PingParams(BaseModel):
    """Heartbeat панели (спека §5): application-level, т.к. браузерный WebSocket
    не умеет отправлять ping-фреймы. ``nonce`` возвращается в ответе для RTT."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["ping"] = "ping"
    nonce: str | None = Field(default=None, max_length=64)


class PingResult(BaseModel):
    """Pong: эхо ``nonce`` + время воркера и текущая ревизия снапшота.

    ``revision`` позволяет панели заметить пропущенные события ``state``
    и запросить ``get_state``.
    """

    model_config = ConfigDict(extra="forbid")

    nonce: str | None = None
    server_time: datetime
    revision: int = Field(ge=0)


# --- реестры -----------------------------------------------------------------

COMMAND_PARAMS: dict[str, type[BaseModel]] = {
    Command.GET_STATE: GetStateParams,
    Command.ADD_ACCOUNT: AddAccountParams,
    Command.UPDATE_ACCOUNT: UpdateAccountParams,
    Command.REMOVE_ACCOUNT: RemoveAccountParams,
    Command.START_SESSION: StartSessionParams,
    Command.STOP_SESSION: StopSessionParams,
    Command.INSTALL_PLUGIN: InstallPluginParams,
    Command.UPDATE_PLUGIN: UpdatePluginParams,
    Command.REMOVE_PLUGIN: RemovePluginParams,
    Command.SET_PLUGIN_CONFIG: SetPluginConfigParams,
    Command.EXPORT_STATE: ExportStateParams,
    Command.IMPORT_STATE: ImportStateParams,
    Command.GET_LOGS: GetLogsParams,
    Command.ROTATE_TOKEN: RotateTokenParams,
    Command.RESTART: RestartParams,
    Command.PING: PingParams,
}
"""``method`` → модель params (payload запроса)."""

COMMAND_RESULTS: dict[str, type[BaseModel]] = {
    Command.GET_STATE: StateWithCredentials,
    Command.ADD_ACCOUNT: AccountRef,
    Command.UPDATE_ACCOUNT: AccountRef,
    Command.REMOVE_ACCOUNT: AccountRef,
    Command.START_SESSION: SessionRef,
    Command.STOP_SESSION: SessionRef,
    Command.INSTALL_PLUGIN: PluginState,
    Command.UPDATE_PLUGIN: PluginState,
    Command.REMOVE_PLUGIN: PluginRef,
    Command.SET_PLUGIN_CONFIG: PluginConfigResult,
    Command.EXPORT_STATE: ExportStateResult,
    Command.IMPORT_STATE: ImportStateResult,
    Command.GET_LOGS: GetLogsResult,
    Command.ROTATE_TOKEN: RotateTokenResult,
    Command.RESTART: RestartResult,
    Command.PING: PingResult,
}
"""``method`` → модель ``result``."""

RequestPayloadT = Annotated[
    GetStateParams
    | AddAccountParams
    | UpdateAccountParams
    | RemoveAccountParams
    | StartSessionParams
    | StopSessionParams
    | InstallPluginParams
    | UpdatePluginParams
    | RemovePluginParams
    | SetPluginConfigParams
    | ExportStateParams
    | ImportStateParams
    | GetLogsParams
    | RotateTokenParams
    | RestartParams
    | PingParams,
    Field(discriminator="method"),
]
"""Дискриминированное объединение payload'ов запросов."""


def encode_result(method: str | Command, result: BaseModel) -> dict[str, Any]:
    """Сериализует result-модель в dict для ``Response.payload.result``.

    Использует :func:`~lucrium_protocol.types.to_wire`: ``get_state``-ответ несёт
    креды аккаунтов открыто (канал аутентифицирован bearer-токеном, см. §10).
    """
    _ensure_known_method(method)
    if type(result) is not COMMAND_RESULTS[str(method)]:
        raise ProtocolError(
            ErrorCode.INTERNAL,
            f"result for {method} must be {COMMAND_RESULTS[str(method)].__name__}, got {type(result).__name__}",
        )
    return to_wire(result)


def decode_result(method: str | Command, data: dict[str, Any]) -> BaseModel:
    """Парсит ``Response.payload.result`` по реестру команд."""
    model = _ensure_known_method(method)
    try:
        return model.model_validate(data)
    except ValueError as exc:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"malformed result for {method}: {exc}") from exc


def _ensure_known_method(method: str | Command) -> type[BaseModel]:
    try:
        return COMMAND_RESULTS[str(method)]
    except KeyError:
        raise ProtocolError(ErrorCode.METHOD_NOT_FOUND, f"unknown method: {method}") from None


__all__ = [
    "COMMAND_PARAMS",
    "COMMAND_RESULTS",
    "MAX_B64_CHARS",
    "MAX_RAW_BYTES",
    "AccountRef",
    "AddAccountParams",
    "Command",
    "ExportStateParams",
    "ExportStateResult",
    "GetLogsParams",
    "GetLogsResult",
    "GetStateParams",
    "ImportMode",
    "ImportStateParams",
    "ImportStateResult",
    "InstallPluginParams",
    "LogScope",
    "PingParams",
    "PingResult",
    "PluginConfigResult",
    "PluginRef",
    "RemoveAccountParams",
    "RemovePluginParams",
    "RequestPayloadT",
    "RestartParams",
    "RestartResult",
    "RestartScope",
    "RotateTokenParams",
    "RotateTokenResult",
    "SessionRef",
    "SetPluginConfigParams",
    "StartSessionParams",
    "StopSessionParams",
    "UpdateAccountParams",
    "UpdatePluginParams",
    "decode_b64",
    "decode_result",
    "encode_result",
]
