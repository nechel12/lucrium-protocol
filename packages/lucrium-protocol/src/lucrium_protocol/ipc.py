"""JSON-RPC 2.0 — контракт IPC «воркер ↔ плагин» (спека §6.3).

Плагин исполняется в **отдельном процессе**; общение с воркером (host) идёт
по JSON-RPC поверх asyncio streams поверх unix socket (путь передаётся плагину
через переменную окружения ``LUCRIUM_PLUGIN_SOCKET``). Фрейминг — **NDJSON**:
одна JSON-строка = одно сообщение, разделитель ``\\n``, лимит строки 16 МиБ.

Направления:

* host → плагин: ``plugin.start`` / ``plugin.event`` / ``plugin.stop`` —
  вызовы контракта ``on_start`` / ``on_event`` / ``on_stop``.
* плагин → host: только через ``ctx`` — ``storage.*``, ``log.write``,
  ``session.call`` (доступ к клиенту aiosellers сессии),
  ``http.request`` (разрешён лишь при ``network = true`` в манифесте).

Любой другой метод — ``METHOD_NOT_FOUND``. Превышение лимитов/timeout —
на стороне host (RLIMIT, таймауты на вызовы, авто-restart с backoff).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import MAX_ERROR_MESSAGE
from .events import HubEvent
from .types import LogLevel

JSONRPC_VERSION = "2.0"

MAX_HTTP_HEADERS = 32
MAX_HTTP_HEADER_NAME_CHARS = 128
MAX_HTTP_HEADER_VALUE_CHARS = 1024
MAX_HTTP_BODY_B64_CHARS = 11 * 1024 * 1024


def _has_control_chars(value: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


# --- host → плагин -----------------------------------------------------------
PLUGIN_START = "plugin.start"
PLUGIN_EVENT = "plugin.event"
PLUGIN_STOP = "plugin.stop"

# --- плагин → host -----------------------------------------------------------
STORAGE_GET = "storage.get"
STORAGE_SET = "storage.set"
STORAGE_DELETE = "storage.delete"
STORAGE_LIST = "storage.list"
LOG_WRITE = "log.write"
SESSION_CALL = "session.call"
HTTP_REQUEST = "http.request"

PLUGIN_TO_HOST_METHODS: frozenset[str] = frozenset(
    {STORAGE_GET, STORAGE_SET, STORAGE_DELETE, STORAGE_LIST, LOG_WRITE, SESSION_CALL, HTTP_REQUEST}
)
HOST_TO_PLUGIN_METHODS: frozenset[str] = frozenset({PLUGIN_START, PLUGIN_EVENT, PLUGIN_STOP})


class RpcErrorCode:
    """Коды ошибок JSON-RPC 2.0 + приложения Lucrium."""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    # application range
    FORBIDDEN = -32001
    TIMEOUT = -32002
    RATE_LIMITED = -32003
    NOT_FOUND = -32004
    UNAVAILABLE = -32005


class RpcError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: int
    message: str = Field(min_length=1, max_length=MAX_ERROR_MESSAGE)
    data: Any | None = None

    @field_validator("message", mode="before")
    @classmethod
    def _truncate_message(cls, value: object) -> str:
        return (str(value) or "error")[:MAX_ERROR_MESSAGE]


class RpcRequest(BaseModel):
    """Запрос JSON-RPC 2.0."""

    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str
    method: str = Field(min_length=1, max_length=200)
    params: dict[str, Any] | None = None


class RpcNotification(BaseModel):
    """Уведомление (без ``id``): ответ на него не отправляется."""

    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"] = "2.0"
    method: str = Field(min_length=1, max_length=200)
    params: dict[str, Any] | None = None


class RpcResponse(BaseModel):
    """Ответ на запрос: ровно один из ``result`` / ``error`` (``result`` может быть ``null``)."""

    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    result: Any | None = None
    error: RpcError | None = None

    @model_validator(mode="after")
    def _one_of(self) -> RpcResponse:
        if self.error is not None and self.result is not None:
            raise ValueError("response must not contain both result and error")
        return self

    @property
    def ok(self) -> bool:
        return self.error is None


# --- params/result модели ----------------------------------------------------


class StartParams(BaseModel):
    """``plugin.start``: конфиг, сгенерированный панелью из JSON-схемы манифеста."""

    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)


class StartResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool = True


class EventParams(BaseModel):
    """``plugin.event``: нормализованный HubEvent из aiosellers."""

    model_config = ConfigDict(extra="forbid")

    event: HubEvent


class EventResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handled: bool = True


class StopParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)


class StopResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stopped: bool = True


class StorageGetParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=512)


class StorageValueResult(BaseModel):
    """``value == None`` — ключа нет."""

    model_config = ConfigDict(extra="forbid")

    value: Any | None = None


class StorageSetParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=512)
    value: Any


class StorageDeleteParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=512)


class StorageListParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefix: str = Field(default="", max_length=512)


class StorageListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: list[str]


class LogWriteParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: LogLevel = LogLevel.INFO
    message: str = Field(max_length=8192)
    fields: dict[str, Any] = Field(default_factory=dict)


class SessionCallParams(BaseModel):
    """Вызов метода клиента aiosellers конкретной сессии (``ctx.session``).

    ``method`` — dotted-путь вида ``chats.get_by_name``, ``lots.fields``;
    host разворачивает его в реальный вызов и нормализует результат в JSON.

    ``session_id`` — к какой сессии обращаться: ``ctx.session`` событийного
    контекста подставляет его сам; ``None`` допустим, только если у плагина
    доступна ровно одна сессия (иначе host вернёт ``INVALID_PARAMS``).
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(default=None, max_length=64, description="id сессии (= account id)")
    method: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z_][\w]*(\.[a-zA-Z_][\w]*)*$")
    params: dict[str, Any] = Field(default_factory=dict)


class SessionCallResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: Any | None = None


class HttpRequestParams(BaseModel):
    """HTTP-запрос от лица воркера — разрешён только при ``permissions.network = true``.

    Границы здесь не косметика: запрос исполняет воркер, и плагин получает
    доступ к сети *его* руками. Egress-политика (запрет loopback, приватных и
    link-local адресов) на стороне host; здесь — разборчивый ввод.
    """

    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "GET"
    url: str = Field(min_length=1, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict, max_length=MAX_HTTP_HEADERS)
    body_b64: str | None = Field(default=None, max_length=MAX_HTTP_BODY_B64_CHARS)
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    @field_validator("headers")
    @classmethod
    def _printable_headers(cls, v: dict[str, str]) -> dict[str, str]:
        for name, value in v.items():
            if not name or len(name) > MAX_HTTP_HEADER_NAME_CHARS:
                raise ValueError(f"header name must be 1-{MAX_HTTP_HEADER_NAME_CHARS} chars")
            if len(value) > MAX_HTTP_HEADER_VALUE_CHARS:
                raise ValueError(f"header {name!r} value is too long")
            if _has_control_chars(name) or _has_control_chars(value):
                raise ValueError(f"header {name!r} must not contain control characters")
        return v

    @field_validator("url")
    @classmethod
    def _absolute_http_url(cls, v: str) -> str:
        lowered = v.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ValueError("url must be absolute http(s)")
        if " " in v:
            raise ValueError("url must not contain spaces")
        if _has_control_chars(v):
            # \r\n внутри url — инъекция заголовка в запрос, который делает воркер
            raise ValueError("url must not contain control characters")
        return v


class HttpResponseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: int = Field(ge=100, le=599)
    headers: dict[str, str] = Field(default_factory=dict, max_length=MAX_HTTP_HEADERS)
    body_b64: str = Field(default="", max_length=MAX_HTTP_BODY_B64_CHARS)


__all__ = [
    "HOST_TO_PLUGIN_METHODS",
    "HTTP_REQUEST",
    "JSONRPC_VERSION",
    "LOG_WRITE",
    "MAX_HTTP_BODY_B64_CHARS",
    "MAX_HTTP_HEADERS",
    "MAX_HTTP_HEADER_NAME_CHARS",
    "MAX_HTTP_HEADER_VALUE_CHARS",
    "PLUGIN_EVENT",
    "PLUGIN_START",
    "PLUGIN_STOP",
    "PLUGIN_TO_HOST_METHODS",
    "SESSION_CALL",
    "STORAGE_DELETE",
    "STORAGE_GET",
    "STORAGE_LIST",
    "STORAGE_SET",
    "EventParams",
    "EventResult",
    "HttpRequestParams",
    "HttpResponseResult",
    "LogWriteParams",
    "RpcError",
    "RpcErrorCode",
    "RpcNotification",
    "RpcRequest",
    "RpcResponse",
    "SessionCallParams",
    "SessionCallResult",
    "StartParams",
    "StartResult",
    "StopParams",
    "StopResult",
    "StorageDeleteParams",
    "StorageGetParams",
    "StorageListParams",
    "StorageListResult",
    "StorageSetParams",
    "StorageValueResult",
]
