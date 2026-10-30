"""Инструменты тестирования плагинов — без воркера, площадок и сокетов.

Позволяет писать юнит-тесты плагина прямо в его каталоге::

    from lucrium_plugin_sdk.testing import run_lifecycle
    from lucrium_plugin_sdk.events import PluginEvent

    result = await run_lifecycle(
        Path(__file__).parent,               # каталог с plugin.toml
        config={"reply_text": "Привет!"},
        events=[PluginEvent(...)],           # сымитированные события площадки
    )
    assert result.session.calls[0].method == "chats.send_message"
    assert "greeting" in result.storage.data

Фейки реализуют те же view-протоколы, что и RPC-реализации SDK
(:class:`~lucrium_plugin_sdk.context.SessionView` и др.), — хуки плагина
не отличают тест от продакшена.

Для тестов самого рантайма есть :class:`FakeHost` — подменяет
:class:`~lucrium_plugin_sdk.transport.JsonRpcClient` в
:class:`~lucrium_plugin_sdk.runtime.PluginRuntime` и эмулирует воркер
в памяти (storage/log/session.call/http).
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from lucrium_protocol.events import HubEvent
from lucrium_protocol.ipc import (
    HTTP_REQUEST,
    LOG_WRITE,
    SESSION_CALL,
    STORAGE_DELETE,
    STORAGE_GET,
    STORAGE_LIST,
    STORAGE_SET,
    HttpRequestParams,
    HttpResponseResult,
    LogWriteParams,
    RpcError,
    RpcNotification,
    RpcRequest,
    RpcResponse,
    SessionCallParams,
    StorageListParams,
    StorageSetParams,
)
from lucrium_protocol.types import LogLevel

from .context import ConfigView, HttpResponse, PluginContext
from .entry import PluginEntry, discover_entry
from .events import PluginEvent
from .manifest import load_manifest
from .runtime import import_entry_module

__all__ = [
    "FakeHost",
    "FakeHttp",
    "FakeSession",
    "HttpRequest",
    "LifecycleResult",
    "ListLog",
    "LogRecord",
    "MemoryStorage",
    "SessionCall",
    "make_context",
    "run_lifecycle",
]


# --- storage -----------------------------------------------------------------


class MemoryStorage:
    """KV-хранилище в памяти — :class:`~lucrium_plugin_sdk.context.StorageView`."""

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = dict(initial or {})

    async def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    async def keys(self, prefix: str = "") -> list[str]:
        return sorted(key for key in self.data if key.startswith(prefix))


# --- log ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogRecord:
    """Одна запись :class:`ListLog`."""

    level: LogLevel
    message: str
    fields: dict[str, Any] = field(default_factory=dict)


class ListLog:
    """Журнал в списке — :class:`~lucrium_plugin_sdk.context.LogView`."""

    def __init__(self) -> None:
        self.records: list[LogRecord] = []

    def _add(self, level: LogLevel, message: str, fields: dict[str, Any]) -> None:
        self.records.append(LogRecord(level=level, message=message, fields=fields))

    def debug(self, message: str, **fields: Any) -> None:
        self._add(LogLevel.DEBUG, message, fields)

    def info(self, message: str, **fields: Any) -> None:
        self._add(LogLevel.INFO, message, fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._add(LogLevel.WARNING, message, fields)

    def error(self, message: str, **fields: Any) -> None:
        self._add(LogLevel.ERROR, message, fields)

    def messages(self, level: LogLevel | None = None) -> list[str]:
        """Тексты записей (опционально — от заданного уровня и выше)."""
        order = [LogLevel.DEBUG, LogLevel.INFO, LogLevel.WARNING, LogLevel.ERROR]
        if level is None:
            return [record.message for record in self.records]
        min_index = order.index(level)
        return [r.message for r in self.records if order.index(r.level) >= min_index]


# --- session -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionCall:
    """Записанный вызов ``ctx.session.call(...)``."""

    session_id: str | None
    method: str
    params: dict[str, Any]


class FakeSession:
    """Сессия-фейк — :class:`~lucrium_plugin_sdk.context.SessionView`.

    Регистрируйте ответы через :meth:`on` (значение или callable/async-callable
    от ``params``); все вызовы записываются в :attr:`calls`. ``for_session``
    возвращает вид с тем же журналом и обработчиками, но другим ``session_id`` —
    так тестируется плагин, обслуживающий несколько аккаунтов.
    """

    def __init__(
        self,
        session_id: str | None = None,
        *,
        calls: list[SessionCall] | None = None,
        handlers: dict[str, Any] | None = None,
    ) -> None:
        self.session_id = session_id
        self.calls: list[SessionCall] = calls if calls is not None else []
        self.handlers: dict[str, Any] = handlers if handlers is not None else {}

    def on(self, method: str, result: Any) -> FakeSession:
        """Ставит ответ на ``method``: готовое значение или ``callable(params)``."""
        self.handlers[method] = result
        return self

    async def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        arguments = dict(params or {})
        self.calls.append(SessionCall(session_id=self.session_id, method=method, params=arguments))
        if method not in self.handlers:
            raise LookupError(f"FakeSession: no handler for {method!r} (register with .on({method!r}, ...))")
        handler = self.handlers[method]
        if callable(handler):
            result = handler(arguments)
            if inspect.isawaitable(result):
                result = await result
            return result
        return handler

    def for_session(self, session_id: str) -> FakeSession:
        return FakeSession(session_id, calls=self.calls, handlers=self.handlers)

    def calls_for(self, method: str) -> list[SessionCall]:
        return [call for call in self.calls if call.method == method]

    def last_call(self, method: str | None = None) -> SessionCall:
        """Последний вызов (опционально — конкретного метода)."""
        calls = self.calls_for(method) if method else self.calls
        if not calls:
            raise LookupError(f"FakeSession: no calls recorded{f' for {method!r}' if method else ''}")
        return calls[-1]


# --- http --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """Записанный ``ctx.http.request(...)``."""

    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout: float | None


class FakeHttp:
    """HTTP-фейк — :class:`~lucrium_plugin_sdk.context.HttpView`.

    Ответы регистрируются по ``(method, url)`` или по url: :meth:`on`.
    Значение — :class:`~lucrium_plugin_sdk.context.HttpResponse`, ``(status, body)``,
    ``bytes``/``str`` (200) или callable от :class:`HttpRequest`.
    """

    def __init__(self, responses: Mapping[tuple[str, str], Any] | None = None) -> None:
        self.requests: list[HttpRequest] = []
        self.responses: dict[tuple[str, str], Any] = dict(responses or {})

    def on(self, url: str, response: Any, *, method: str = "GET") -> FakeHttp:
        self.responses[(method.upper(), url)] = response
        return self

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        record = HttpRequest(
            method=method.upper(),
            url=url,
            headers=dict(headers or {}),
            body=body,
            timeout=timeout,
        )
        self.requests.append(record)
        key = (record.method, url)
        if key not in self.responses and (record.method, "*") in self.responses:
            key = (record.method, "*")
        if key not in self.responses:
            raise LookupError(f"FakeHttp: no response registered for {key} (register with .on(...))")
        handler = self.responses[key]
        value = handler(record) if callable(handler) else handler
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, HttpResponse):
            return value
        if isinstance(value, tuple):
            status, raw = value
            return HttpResponse(status, {}, _as_bytes(raw))
        return HttpResponse(200, {}, _as_bytes(value))


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode()
    return json.dumps(value, ensure_ascii=False).encode()


# --- контекст ----------------------------------------------------------------


def make_context(
    plugin_id: str = "test-plugin",
    *,
    config: Mapping[str, Any] | None = None,
    session: FakeSession | None = None,
    storage: MemoryStorage | None = None,
    log: ListLog | None = None,
    http: FakeHttp | None = None,
    session_id: str | None = None,
) -> PluginContext:
    """Собирает :class:`~lucrium_plugin_sdk.context.PluginContext` из фейков —
    удобно для тестов отдельных хуков без запуска жизненного цикла."""
    return PluginContext(
        plugin_id=plugin_id,
        config=ConfigView(dict(config or {})),
        session=session if session is not None else FakeSession(session_id),
        storage=storage if storage is not None else MemoryStorage(),
        log=log if log is not None else ListLog(),
        http=http if http is not None else FakeHttp(),
        session_id=session_id,
    )


# --- прогон жизненного цикла -------------------------------------------------


@dataclass(slots=True)
class LifecycleResult:
    """Результат :func:`run_lifecycle` — все побочные эффекты хуков."""

    log: ListLog
    storage: MemoryStorage
    session: FakeSession
    http: FakeHttp
    started: bool = False
    stopped: bool = False
    start_error: BaseException | None = None
    event_errors: list[BaseException] = field(default_factory=list)
    stop_error: BaseException | None = None


async def run_lifecycle(
    plugin: str | Path | ModuleType | object,
    *,
    config: Mapping[str, Any] | None = None,
    events: Iterable[PluginEvent | HubEvent] = (),
    session: FakeSession | None = None,
    storage: MemoryStorage | None = None,
    log: ListLog | None = None,
    http: FakeHttp | None = None,
) -> LifecycleResult:
    """Прогоняет ``on_start`` → ``on_event``* → ``on_stop`` с фейками.

    ``plugin`` — каталог плагина (путь с ``plugin.toml``) или модуль/объект
    с хуками. Ошибки хуков не поднимаются, а копятся в результате (как в
    рантайме: крах события не убивает плагин); ``start_error`` — исключение
    ``on_start`` (в проде это ненулевой код выхода и рестарт с backoff).
    """
    if isinstance(plugin, str | Path):
        directory = Path(plugin)
        manifest = load_manifest(directory)
        module: object = import_entry_module(directory, manifest.entry)
    else:
        module = plugin
    entry: PluginEntry = discover_entry(module).ensure_valid()

    fake_session = session if session is not None else FakeSession()
    fake_storage = storage if storage is not None else MemoryStorage()
    fake_log = log if log is not None else ListLog()
    fake_http = http if http is not None else FakeHttp()
    result = LifecycleResult(log=fake_log, storage=fake_storage, session=fake_session, http=fake_http)

    def context_for(session_id: str | None) -> PluginContext:
        return PluginContext(
            plugin_id=getattr(module, "__name__", "test-plugin"),
            config=ConfigView(dict(config or {})),
            session=fake_session.for_session(session_id) if session_id else fake_session,
            storage=fake_storage,
            log=fake_log,
            http=fake_http,
            session_id=session_id,
        )

    if entry.on_start is not None:
        try:
            await entry.on_start(context_for(None))
            result.started = True
        except Exception as exc:
            result.start_error = exc

    if entry.on_event is not None:
        for raw_event in events:
            event = raw_event if isinstance(raw_event, PluginEvent) else PluginEvent.from_hub(raw_event)
            try:
                await entry.on_event(event, context_for(event.session_id))
            except Exception as exc:
                result.event_errors.append(exc)

    if entry.on_stop is not None:
        try:
            await entry.on_stop(context_for(None))
            result.stopped = True
        except Exception as exc:
            result.stop_error = exc

    return result


# --- fake host (для тестов рантайма) ----------------------------------------


class FakeHost:
    """Эмулятор воркера для :class:`~lucrium_plugin_sdk.runtime.PluginRuntime`.

    Подставляется вместо :class:`~lucrium_plugin_sdk.transport.JsonRpcClient`:
    ``recv`` выдаёт подготовленные сообщения host → плагин, ``respond``/``notify``
    записываются. Запросы плагина → host (``storage.*``, ``session.call``,
    ``log.write``, ``http.request``) обслуживаются в памяти.

    Пример::

        host = FakeHost()
        runtime = PluginRuntime(manifest, entry, host)  # type: ignore[arg-type]
        await host.send_request("plugin.start", {"plugin_id": "p", "config": {}})
        task = asyncio.create_task(runtime.run())
        await host.wait_for_response()   # старт подтверждён
        await host.send_request("plugin.event", {"event": hub_event.model_dump(mode="json")})
        ...
    """

    def __init__(
        self,
        *,
        session_data: Any = None,
        http_response: HttpResponseResult | None = None,
    ) -> None:
        self._inbox: list[RpcRequest | RpcNotification] = []
        self.responses: list[RpcResponse] = []
        self.sent_notifications: list[RpcNotification] = []
        self.log_entries: list[LogWriteParams] = []
        self.storage: dict[str, Any] = {}
        self.session_calls: list[SessionCallParams] = []
        self.http_requests: list[HttpRequestParams] = []
        self.session_data: Any = session_data
        self.http_response = http_response or HttpResponseResult(status=200, body_b64="")
        self.closed = False

    # --- вход host → плагин --------------------------------------------------

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> RpcRequest:
        request = RpcRequest(id=len(self._inbox) + 1, method=method, params=params)
        self._inbox.append(request)
        return request

    def send_notification(self, method: str, params: dict[str, Any] | None = None) -> RpcNotification:
        notification = RpcNotification(method=method, params=params)
        self._inbox.append(notification)
        return notification

    async def wait_for_response(self, timeout: float = 5.0) -> RpcResponse:
        """Ждёт следующий ответ плагина (используйте после send_*)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while not self.responses:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("FakeHost: no response from plugin")
            await asyncio.sleep(0.001)
        return self.responses.pop(0)

    # --- интерфейс JsonRpcClient (только используемое рантаймом) -------------

    async def recv(self) -> RpcRequest | RpcNotification:
        while not self._inbox:
            await asyncio.sleep(0.001)
        return self._inbox.pop(0)

    async def respond(
        self,
        request: RpcRequest,
        *,
        result: Any = None,
        error: RpcError | None = None,
    ) -> None:
        self.responses.append(RpcResponse(id=request.id, result=result, error=error))

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.sent_notifications.append(RpcNotification(method=method, params=params))
        if method == LOG_WRITE and params is not None:
            # ctx.log шлёт log.write как notify — для удобства тестов дублируем в log_entries
            self.log_entries.append(LogWriteParams.model_validate(params))

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Запрос плагина → host: обслуживается в памяти."""
        arguments = params or {}
        if method == STORAGE_GET:
            return {"value": self.storage.get(arguments["key"])}
        if method == STORAGE_SET:
            parsed = StorageSetParams.model_validate(arguments)
            self.storage[parsed.key] = parsed.value
            return {}
        if method == STORAGE_DELETE:
            self.storage.pop(arguments["key"], None)
            return {}
        if method == STORAGE_LIST:
            prefix = StorageListParams.model_validate(arguments).prefix
            return {"keys": sorted(k for k in self.storage if k.startswith(prefix))}
        if method == LOG_WRITE:
            self.log_entries.append(LogWriteParams.model_validate(arguments))
            return {}
        if method == SESSION_CALL:
            call = SessionCallParams.model_validate(arguments)
            self.session_calls.append(call)
            if callable(self.session_data):
                value = self.session_data(call)
                if inspect.isawaitable(value):
                    value = await value
                return {"data": value}
            return {"data": self.session_data}
        if method == HTTP_REQUEST:
            parsed_http = HttpRequestParams.model_validate(arguments)
            self.http_requests.append(parsed_http)
            return self.http_response.model_dump(mode="json")
        raise LookupError(f"FakeHost: unknown method {method!r}")

    async def close(self) -> None:
        self.closed = True
