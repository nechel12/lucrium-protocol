"""NDJSON JSON-RPC 2.0 клиент поверх asyncio streams (unix socket).

Контракт: см. :mod:`lucrium_protocol.ipc`. Фрейминг — одна JSON-строка на
сообщение (``\\n``-разделитель). ``JsonRpcClient`` совмещает три роли:

* отправка ``request`` с ожиданием ответа по ``id`` (``asyncio.Future``);
* отправка ``notify`` (без ответа);
* очередь входящих ``request``/``notify`` от host (``recv``) — её потребляет
  runtime плагина.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import Awaitable, Callable
from typing import Any

from lucrium_protocol.ipc import RpcError, RpcNotification, RpcRequest, RpcResponse

MAX_LINE_BYTES = 16 * 1024 * 1024
CONNECT_TIMEOUT = 5.0
DEFAULT_REQUEST_TIMEOUT = 30.0

_SENTINEL = object()  # сигнал «соединение закрыто» внутри inbox


class TransportError(Exception):
    """Связь с host оборвалась или нарушена."""


class RpcRemoteError(TransportError):
    """Host вернул ``error`` на наш запрос."""

    def __init__(self, error: RpcError) -> None:
        super().__init__(f"{error.code}: {error.message}")
        self.code = error.code
        self.message = error.message
        self.data = error.data


InboxMessage = RpcRequest | RpcNotification | None


class JsonRpcClient:
    """Клиент одной IPC-сессии плагина."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._inbox: asyncio.Queue[InboxMessage] = asyncio.Queue()
        self._ids = itertools.count(1)
        self._closed = False
        self._closing = False
        self._read_task = asyncio.create_task(self._read_loop(), name="lucrium-plugin-rpc-read")

    @classmethod
    async def connect_unix(
        cls,
        path: str,
        *,
        timeout: float = CONNECT_TIMEOUT,
    ) -> JsonRpcClient:
        """Подключается к unix socket воркера (host уже слушает)."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(path, limit=MAX_LINE_BYTES),
                timeout=timeout,
            )
        except (TimeoutError, OSError) as exc:
            raise TransportError(f"cannot connect to worker socket {path!r}: {exc}") from exc
        return cls(reader, writer)

    @property
    def closed(self) -> bool:
        return self._closed

    # --- исходящие -----------------------------------------------------------

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> Any:
        """Отправляет запрос и ждёт ``result``; ``RpcRemoteError`` при ``error`` host."""
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send(RpcRequest(id=request_id, method=method, params=params))
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            future.cancel()
            raise TransportError(f"rpc timeout waiting for {method!r} after {timeout}s") from exc
        except Exception:
            self._pending.pop(request_id, None)
            raise

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Отправляет уведомление (без ожидания ответа)."""
        await self._send(RpcNotification(method=method, params=params))

    async def respond(
        self,
        request: RpcRequest,
        *,
        result: Any = None,
        error: RpcError | None = None,
    ) -> None:
        """Отвечает host'у на его запрос к плагину."""
        await self._send(RpcResponse(id=request.id, result=result, error=error))

    # --- входящие ------------------------------------------------------------

    async def recv(self) -> RpcRequest | RpcNotification:
        """Следующее входящее сообщение от host; ``TransportError`` при обрыве."""
        message = await self._inbox.get()
        if message is None:
            raise TransportError("worker closed the connection")
        return message

    # --- служебное -----------------------------------------------------------

    async def close(self) -> None:
        # _closed может быть уже выставлен _read_loop при обрыве/ошибке парсера —
        # writer всё равно обязан закрыться, иначе сокет останется висеть
        if self._closing:
            return
        self._closing = True
        self._closed = True
        self._read_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._read_task
        self._writer.close()
        with contextlib.suppress(OSError):
            await self._writer.wait_closed()
        self._fail_pending(TransportError("connection closed"))
        if self._inbox.empty():
            self._inbox.put_nowait(None)

    async def _send(self, message: RpcRequest | RpcNotification | RpcResponse) -> None:
        if self._closed:
            raise TransportError("connection closed")
        line = message.model_dump_json(exclude_none=True) + "\n"
        try:
            self._writer.write(line.encode())
            await self._writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            raise TransportError(f"cannot send to worker: {exc}") from exc

    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self._reader.readline()
                if not raw:
                    break  # EOF
                self._dispatch(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # парсер/лимиты — ломаем соединение, runtime сообщит наверх
            self._inbox.put_nowait(None)
            self._fail_pending(TransportError(f"malformed stream from worker: {exc}"))
            self._closed = True
            return
        # EOF
        self._closed = True
        self._fail_pending(TransportError("worker closed the connection"))
        self._inbox.put_nowait(None)

    def _dispatch(self, raw: bytes) -> None:
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TransportError(f"invalid JSON from worker: {exc}") from exc
        if not isinstance(data, dict):
            raise TransportError("worker message must be a JSON object")

        if "method" in data:
            if "id" in data:
                self._inbox.put_nowait(RpcRequest.model_validate(data))
            else:
                self._inbox.put_nowait(RpcNotification.model_validate(data))
            return

        # ответ на наш запрос
        try:
            response = RpcResponse.model_validate(data)
        except ValueError as exc:
            raise TransportError(f"invalid response from worker: {exc}") from exc
        if response.id is None:
            raise TransportError("response without id")
        future = self._pending.pop(int(response.id), None)
        if future is None or future.done():
            return  # опоздал/повтор — игнорируем
        if response.error is not None:
            future.set_exception(RpcRemoteError(response.error))
        else:
            future.set_result(response.result)

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


# удобный алиас для типизации колбэков runtime'а
RequestHandler = Callable[[RpcRequest], Awaitable[None]]

__all__ = [
    "CONNECT_TIMEOUT",
    "DEFAULT_REQUEST_TIMEOUT",
    "MAX_LINE_BYTES",
    "InboxMessage",
    "JsonRpcClient",
    "RequestHandler",
    "RpcRemoteError",
    "TransportError",
]
