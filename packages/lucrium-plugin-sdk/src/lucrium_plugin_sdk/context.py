"""``PluginContext`` — единственный выход плагина наружу (спека §6.1).

``ctx`` даёт доступ к::

    ctx.session  — клиент aiosellers сессии (только через воркер)
    ctx.config   — типизированный конфиг из JSON-схемы манифеста
    ctx.storage  — персистентный KV плагина
    ctx.log      — журнал (живьём в панель + файл воркера)
    ctx.http     — прямой HTTP (только при permissions.network = true; решает воркер)

Плагин **не** трогает площадки и сеть напрямую. Все вызовы — асинхронные
и проходят по IPC в воркер (``lucrium_protocol.ipc``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

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
    SessionCallParams,
    StorageListParams,
    StorageSetParams,
)
from lucrium_protocol.types import LogLevel

from .transport import JsonRpcClient


class PluginUsageError(RuntimeError):
    """Некорректное использование контекста (например, сессия не привязана)."""


# --- view-интерфейсы (реализуют и рантайм, и фейки для тестов) ----------------


@runtime_checkable
class SessionView(Protocol):
    """Обёртка над клиентом aiosellers одной сессии (``ctx.session``)."""

    async def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """Вызывает ``client.<method>(**params)`` в воркере и возвращает JSON-результат."""
        ...

    def for_session(self, session_id: str) -> SessionView:
        """Тот же шлюз, привязанный к другой сессии (плагин один на все аккаунты)."""
        ...


@runtime_checkable
class StorageView(Protocol):
    """Персистентный KV плагина (``ctx.storage``)."""

    async def get(self, key: str, default: Any = None) -> Any: ...

    async def set(self, key: str, value: Any) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def keys(self, prefix: str = "") -> list[str]: ...


@runtime_checkable
class LogView(Protocol):
    """Журнал плагина (``ctx.log``)."""

    def debug(self, message: str, **fields: Any) -> None: ...

    def info(self, message: str, **fields: Any) -> None: ...

    def warning(self, message: str, **fields: Any) -> None: ...

    def error(self, message: str, **fields: Any) -> None: ...


class HttpResponse:
    """Ответ ``ctx.http.request``."""

    __slots__ = ("_body", "headers", "status")

    def __init__(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
        self.status = status
        self.headers = dict(headers)
        self._body = body

    @property
    def body(self) -> bytes:
        return self._body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self, encoding: str = "utf-8") -> str:
        return self._body.decode(encoding, errors="replace")

    def json(self) -> Any:
        return json.loads(self._body)

    def __repr__(self) -> str:
        return f"HttpResponse(status={self.status}, bytes={len(self._body)})"


@runtime_checkable
class HttpView(Protocol):
    """Прямой HTTP из плагина (``ctx.http``); разрешение — ``permissions.network``."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> HttpResponse: ...


# --- конфиг ------------------------------------------------------------------


class _PathView:
    """Доступ к вложенным ключам по точечному пути: ``cfg.get("ai.model")``."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = dict(data)

    @staticmethod
    def _dig(data: Any, parts: Iterable[str]) -> Any:
        current = data
        for part in parts:
            if isinstance(current, Mapping):
                current = current.get(part)
            else:
                return None
        return current

    def get(self, path: str, default: Any = None) -> Any:
        value = self._dig(self._data, path.split("."))
        return default if value is None else value

    def require(self, path: str) -> Any:
        value = self._dig(self._data, path.split("."))
        if value is None:
            raise KeyError(f"config key {path!r} is missing")
        return value


class ConfigView(_PathView):
    """Типизированный конфиг плагина (то, что панель собрала из JSON-схемы)."""

    def __getitem__(self, key: str) -> Any:
        value = self._dig(self._data, key.split("."))
        if value is None:
            raise KeyError(key)
        return value

    def __contains__(self, key: str) -> bool:
        return self._dig(self._data, key.split(".")) is not None

    @property
    def data(self) -> dict[str, Any]:
        """Копия сырых данных (для логов/отладки — не содержит того, чего нет в схеме)."""
        return dict(self._data)

    def __repr__(self) -> str:
        return f"ConfigView({self._data!r})"


# --- контекст ----------------------------------------------------------------


@dataclass(slots=True)
class PluginContext:
    """Контекст, передаваемый во все хуки плагина."""

    plugin_id: str
    config: ConfigView
    session: SessionView
    storage: StorageView
    log: LogView
    http: HttpView
    session_id: str | None = field(
        default=None,
        metadata={"description": "сессия события; None для on_start/on_stop"},
    )

    def session_for(self, session_id: str) -> SessionView:
        """Доступ к другой сессии (в пределах permissions.accounts манифеста)."""
        return self.session.for_session(session_id)


# --- RPC-реализации view (используются рантаймом) ----------------------------


class RpcSession:
    """``SessionView`` поверх IPC: ``session.call`` → воркер → aiosellers."""

    __slots__ = ("_client", "_session_id")

    def __init__(self, client: JsonRpcClient, session_id: str | None = None) -> None:
        self._client = client
        self._session_id = session_id

    async def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        payload = SessionCallParams(
            session_id=self._session_id,
            method=method,
            params=dict(params or {}),
        )
        result = await self._client.request(SESSION_CALL, payload.model_dump(mode="json"))
        return result.get("data") if isinstance(result, dict) else result

    def for_session(self, session_id: str) -> RpcSession:
        return RpcSession(self._client, session_id)


class RpcStorage:
    """``StorageView`` поверх IPC."""

    __slots__ = ("_client",)

    def __init__(self, client: JsonRpcClient) -> None:
        self._client = client

    async def get(self, key: str, default: Any = None) -> Any:
        result = await self._client.request(STORAGE_GET, {"key": key})
        value = result.get("value") if isinstance(result, dict) else None
        return default if value is None else value

    async def set(self, key: str, value: Any) -> None:
        payload = StorageSetParams(key=key, value=value)
        await self._client.request(STORAGE_SET, payload.model_dump(mode="json"))

    async def delete(self, key: str) -> None:
        await self._client.request(STORAGE_DELETE, {"key": key})

    async def keys(self, prefix: str = "") -> list[str]:
        result = await self._client.request(STORAGE_LIST, StorageListParams(prefix=prefix).model_dump())
        return list(result.get("keys", [])) if isinstance(result, dict) else []


class RpcLog:
    """``LogView`` поверх IPC: уведомления ``log.write`` (fire-and-forget)."""

    __slots__ = ("_client", "_plugin_id")

    def __init__(self, client: JsonRpcClient, plugin_id: str) -> None:
        self._client = client
        self._plugin_id = plugin_id

    def _write(self, level: LogLevel, message: str, fields: dict[str, Any]) -> None:
        params = LogWriteParams(level=level, message=message[:8192], fields=fields)
        # source воркер знает по IPC-соединению (LogWriteParams — extra="forbid")
        payload = params.model_dump(mode="json")

        async def _send_safely() -> None:
            with contextlib.suppress(Exception):
                # best effort: при обрыве IPC журнал падает молча,
                # воркер сам залогирует обрыв соединения
                await self._client.notify(LOG_WRITE, payload)

        # журнал — fire-and-forget: не должен ронять плагин при обрыве IPC
        with contextlib.suppress(RuntimeError):
            # RuntimeError = нет running loop (вне рантайма) — молча пропускаем
            asyncio.get_running_loop().create_task(_send_safely(), name=f"lucrium-log-{self._plugin_id}")

    def debug(self, message: str, **fields: Any) -> None:
        self._write(LogLevel.DEBUG, message, fields)

    def info(self, message: str, **fields: Any) -> None:
        self._write(LogLevel.INFO, message, fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._write(LogLevel.WARNING, message, fields)

    def error(self, message: str, **fields: Any) -> None:
        self._write(LogLevel.ERROR, message, fields)


class RpcHttp:
    """``HttpView`` поверх IPC (воркер применяет ``permissions.network``)."""

    __slots__ = ("_client",)

    def __init__(self, client: JsonRpcClient) -> None:
        self._client = client

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        import base64

        params = HttpRequestParams(
            method=method.upper(),  # type: ignore[arg-type]
            url=url,
            headers=dict(headers or {}),
            body_b64=base64.b64encode(body).decode() if body is not None else None,
            **({"timeout_seconds": timeout} if timeout is not None else {}),
        )
        result = await self._client.request(HTTP_REQUEST, params.model_dump(mode="json"))
        if not isinstance(result, dict):
            raise RuntimeError(f"malformed http result: {result!r}")
        parsed = HttpResponseResult.model_validate(result)
        return HttpResponse(
            parsed.status,
            parsed.headers,
            base64.b64decode(parsed.body_b64) if parsed.body_b64 else b"",
        )


__all__ = [
    "ConfigView",
    "HttpResponse",
    "HttpView",
    "LogView",
    "PluginContext",
    "PluginUsageError",
    "RpcHttp",
    "RpcLog",
    "RpcSession",
    "RpcStorage",
    "SessionView",
    "StorageView",
]
