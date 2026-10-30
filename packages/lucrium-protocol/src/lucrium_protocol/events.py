"""События «воркер → панель» (спека §5).

Асинхронный поток поверх того же WSS-соединения: ``state`` (снапшот),
``status`` (heartbeat), ``event`` (нормализованный HubEvent из aiosellers),
``plugin_log``, ``plugin_error``, ``command_result`` (асинхронный результат
долгих команд).
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import ErrorCode, ErrorObject, ProtocolError
from .state import StateSnapshot, WorkerStatus
from .types import EventKind, LogEntry, Platform


class ServerEvent(StrEnum):
    """Имена событий в ``payload.event``."""

    STATE = "state"
    STATUS = "status"
    HUB = "event"
    PLUGIN_LOG = "plugin_log"
    PLUGIN_ERROR = "plugin_error"
    COMMAND_RESULT = "command_result"


class HubEvent(BaseModel):
    """Нормализованный HubEvent из ``aiosellers`` (спека §5, §13).

    ``payload`` — сериализованный оригинальный typed event площадки
    (``NewMessageEvent``/``NewDealEvent``/…), ``kind`` — переносимая категория
    (см. :class:`~lucrium_protocol.types.EventKind`).
    """

    model_config = ConfigDict(extra="forbid")

    platform: Platform
    session_id: str = Field(min_length=1, max_length=64, description="id сессии воркера (= account id)")
    kind: EventKind
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(description="время события (UTC)")


class PluginError(BaseModel):
    """Событие ``plugin_error``: крах плагина + план рестарта (спека §6.3)."""

    model_config = ConfigDict(extra="forbid")

    plugin_id: str
    ts: datetime
    error: str = Field(min_length=1, max_length=2000)
    traceback: str | None = Field(default=None, max_length=16384)
    will_restart: bool
    restart_in_seconds: float | None = Field(default=None, ge=0)


class CommandResult(BaseModel):
    """Событие ``command_result``: асинхронное завершение долгой команды.

    Для мгновенных команд результат приходит в ``Response``; для долгих
    (установка плагина, импорт состояния) — сюда, с ``command_id`` = id запроса.
    """

    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1, description="id исходного request-envelope")
    method: str = Field(min_length=1, max_length=64)
    ok: bool
    result: dict[str, Any] | None = None
    error: ErrorObject | None = None
    finished_at: datetime

    @model_validator(mode="after")
    def _consistent(self) -> CommandResult:
        if self.ok and self.error is not None:
            raise ValueError("ok result must not carry error")
        if not self.ok and self.error is None:
            raise ValueError("failed result must carry error")
        return self


# --- payload-обёртки ---------------------------------------------------------


class StatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["state"] = "state"
    data: StateSnapshot


class StatusPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["status"] = "status"
    data: WorkerStatus


class HubPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["event"] = "event"
    data: HubEvent


class PluginLogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["plugin_log"] = "plugin_log"
    data: LogEntry


class PluginErrorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["plugin_error"] = "plugin_error"
    data: PluginError


class CommandResultPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["command_result"] = "command_result"
    data: CommandResult


EventPayloadT = Annotated[
    StatePayload | StatusPayload | HubPayload | PluginLogPayload | PluginErrorPayload | CommandResultPayload,
    Field(discriminator="event"),
]
"""Дискриминированное объединение payload'ов событий."""

EVENT_DATA: dict[str, type[BaseModel]] = {
    ServerEvent.STATE: StateSnapshot,
    ServerEvent.STATUS: WorkerStatus,
    ServerEvent.HUB: HubEvent,
    ServerEvent.PLUGIN_LOG: LogEntry,
    ServerEvent.PLUGIN_ERROR: PluginError,
    ServerEvent.COMMAND_RESULT: CommandResult,
}
"""``event`` → модель ``data``."""


def encode_event_data(event: str | ServerEvent, data: BaseModel) -> dict[str, Any]:
    """Сериализует data-модель события (с проверкой соответствия реестру)."""
    model = _ensure_known_event(event)
    if type(data) is not model:
        raise ProtocolError(
            ErrorCode.INTERNAL,
            f"data for {event} must be {model.__name__}, got {type(data).__name__}",
        )
    return data.model_dump(mode="json")


def decode_event_data(event: str | ServerEvent, data: dict[str, Any]) -> BaseModel:
    """Парсит ``payload.data`` события по реестру."""
    model = _ensure_known_event(event)
    try:
        return model.model_validate(data)
    except ValueError as exc:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"malformed data for {event}: {exc}") from exc


def _ensure_known_event(event: str | ServerEvent) -> type[BaseModel]:
    try:
        return EVENT_DATA[str(event)]
    except KeyError:
        raise ProtocolError(ErrorCode.METHOD_NOT_FOUND, f"unknown event: {event}") from None


def dumps_data(data: BaseModel) -> str:
    """JSON-представление data-модели (для логов/диагностики)."""
    return json.dumps(data.model_dump(mode="json"), ensure_ascii=False)


__all__ = [
    "EVENT_DATA",
    "CommandResult",
    "CommandResultPayload",
    "EventPayloadT",
    "HubEvent",
    "HubPayload",
    "PluginError",
    "PluginErrorPayload",
    "PluginLogPayload",
    "ServerEvent",
    "StatePayload",
    "StatusPayload",
    "decode_event_data",
    "dumps_data",
    "encode_event_data",
]
